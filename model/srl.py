# Syntax-agnostic Semantic Role Labeling (based on Marcheggiani et al, 2017)
#   https://arxiv.org/pdf/1701.02593.pdf
#   https://github.com/diegma/neural-dep-srl
from __future__ import print_function
from __future__ import division

import sys, os
import numpy as np
import tensorflow as tf

import layers, lstm, stags
sys.path.append(os.getcwd())
from util.data_loader import batch_producer
from tensorflow.python.client import timeline
from timeit import default_timer as timer

import traceback

class Redirect(object):
    def __init__(self):    
        self.stdout = sys.stdout
    def write(self, s):
        pass


class SRL_Model(object):
    def __init__(self, vocabs, args):
        self.args = args
        batch_size = args.batch_size

        # Input placeholders
        ## Inputs are shaped (batch_size, seq_length) unless the input
        ##   applies to the whole sentence (e.g. predicate)
        ## words: sequences of word ids
        ## pos: predicted parts of speech
        ## lemmas: lemma ids for the predicates, 0's for the other words
        ## preds: integer id of the predicate in the sentence
        ## preds_idx: the index (position) of the predicate in the sentence
        ## labels: semantic role label for each word in the sequence
        ## labels_mask: mask invalid arg labels (given the predicate)
        ## stags_placeholder: a UD supertag for each word
        ## use_dropout_placeholder: 0.0 or 1.0, whether or not to use dropout
        words_placeholder = tf.placeholder(tf.int32, shape=(batch_size, None))
        freqs_placeholder = tf.placeholder(tf.int32, shape=(batch_size, None))
        pos_placeholder = tf.placeholder(tf.int32, shape=(batch_size, None))
        lemmas_placeholder = tf.placeholder(tf.int32, shape=(batch_size, None))
        preds_placeholder = tf.placeholder(tf.int32, shape=(batch_size,))
        preds_idx_placeholder = tf.placeholder(tf.int32, shape=(batch_size,))
        labels_placeholder = tf.placeholder(tf.int32, shape=(batch_size, None))
        labels_mask_placeholder = tf.placeholder(
            tf.float32, shape=(batch_size, vocabs['labels'].size))
        stags_placeholder = tf.placeholder(tf.int32, shape=(batch_size, None))
        use_dropout_placeholder = tf.placeholder(tf.float32, shape=())
        seq_lengths_placeholder = tf.placeholder(tf.int32, shape=(batch_size,))
        elmo_placeholder = tf.placeholder(tf.string, shape=(batch_size, None))

        # Word representation

        ## Word dropout
        if args.use_word_dropout:
            words = layers.word_dropout(
                words=words_placeholder,
                freqs=freqs_placeholder,
                alpha=args.alpha,
                unk_idx=vocabs['words'].unk_idx,
                use_dropout=use_dropout_placeholder)
        else:
            words = words_placeholder

        ## Trainable word embeddings
        word_embeddings = layers.embed_inputs(
            raw_inputs=words,
            vocab_size=vocabs['words'].size,
            embed_size=args.word_embed_size,
            name='word_embedding')
        elmo_embeddings = layers.add_elmo(elmo_placeholder, seq_lengths_placeholder)

        ## Pretrained word embeddings
        pretr_word_vectors = layers.get_word_embeddings(
            args.language,
            vocabs['words'].idx_to_word, args.word_embed_size)
        pretr_embed_size = pretr_word_vectors.shape[1]
        pretr_word_embeddings = layers.embed_inputs(
            raw_inputs=words,
            vocab_size=vocabs['words'].size,
            embed_size=pretr_embed_size,
            name='pretr_word_embedding',
            embeddings=pretr_word_vectors)

        ## POS embeddings
        pos_embeddings = layers.embed_inputs(
            raw_inputs=pos_placeholder,
            vocab_size=vocabs['pos'].size,
            embed_size=args.pos_embed_size,
            name='pos_embedding')

        ## Lemma embeddings for predicates (0's for non-predicates)
        lemma_embeddings = layers.embed_inputs(
            raw_inputs=lemmas_placeholder,
            vocab_size=vocabs['lemmas'].size,
            embed_size=args.lemma_embed_size,
            name='lemma_embedding')

        word_features = [word_embeddings, pretr_word_embeddings,
                         pos_embeddings, lemma_embeddings, elmo_embeddings]

        ## Supertag embeddings
        if args.use_stags:
            stag_embeddings = layers.embed_inputs(
                raw_inputs=stags_placeholder,
                vocab_size=vocabs['stags'].size,
                embed_size=args.stag_embed_size,
                name='stag_embedding')
            if args.use_stag_features:
                stag_feats = stags.get_model1_embeddings(
                    args.language,
                    vocabs['stags'],
                    args.stag_feature_embed_size)
                stag_feat_embeddings = tf.nn.embedding_lookup(
                    stag_feats, stags_placeholder)
                stag_embeddings = tf.concat([stag_embeddings,
                                             stag_feat_embeddings],
                                            axis=2)
            word_features.append(stag_embeddings)
         
        ## Binary flags to mark the predicate
        seq_length = tf.shape(words_placeholder)[1]
        pred_markers = tf.expand_dims(tf.one_hot(preds_idx_placeholder,
                                                 seq_length,
                                                 dtype=tf.float32),
                                      axis=-1)
        word_features.append(pred_markers)
        
        ## Concatenate all the word features on the last dimension
        inputs = tf.concat(word_features, axis=2)
        input_size = inputs.shape[2]
        
        # BiLSTM

        ## (num_steps, batch_size, embed_size)
        ## num_steps has to be first because LSTM scans over the 1st dimension
        lstm_inputs = tf.transpose(inputs, perm=[1,0,2])

        ## use_dropout_placeholder is 0 or 1, so this just turns dropout
        ## on or off
        dropout = 1.0 - (1.0 - args.dropout) * use_dropout_placeholder
        recurrent_dropout = (1.0 - (1.0 - args.recurrent_dropout) *
                             use_dropout_placeholder)

        if args.use_highway_lstm:
            cell = lstm.HighwayLSTMCell
        else:
            cell = lstm.LSTMCell

        bilstm = lstm.BiLSTM(
            cell=cell,
            input_size=input_size,
            state_size=args.state_size,
            batch_size=args.batch_size,
            num_layers=args.num_layers,
            dropout=dropout,
            recurrent_dropout=recurrent_dropout)
        
        lstm_outputs = bilstm(lstm_inputs)

        ## Transpose back to (batch_size, num_steps, embed_size)
        outputs = tf.transpose(lstm_outputs, perm=[1, 0, 2])


        # Projection

        ## Get the output state corresponding to the predicate in each sentence
        ## outputs is shaped (batch_size, seq_length, output_size)
        ## so pred_outputs is (batch_size, output_size)
        indices = tf.stack([tf.range(batch_size, dtype=tf.int32),
                            preds_idx_placeholder], axis=1)
        pred_outputs = tf.gather_nd(outputs, indices)

        ## Concatenate predicate state with all the other output states.
        ## (It is possible to do this more efficiently without tiling but
        ## it would be more complicated.)
        tiled_pred_outputs = tf.tile(tf.expand_dims(pred_outputs, 1),
                                     (1, seq_length, 1))
        combined_outputs = tf.concat([outputs, tiled_pred_outputs], axis=2)

        ## (2 LSTMs for word, 2 for pred)
        lstm_output_size = args.state_size * 4            
        

        ## Compose role and (output) pred embeddings to get projection weights
        ## (see section 2.4.3 of Marcheggiani et al 2017)
        with tf.variable_scope('projection'):
            
            # Get embedding for labels (roles) and predicates
            num_roles = vocabs['labels'].size
            
            ## (num_roles, r_embed_size)
            role_embeddings = tf.get_variable(
                'role_embeddings',
                shape=(num_roles, args.role_embed_size),
                initializer=tf.orthogonal_initializer(),
                dtype=tf.float32)
            ## (batch_size, p_embed_size)
            pred_embeddings = layers.embed_inputs(
                raw_inputs=preds_placeholder,
                vocab_size=vocabs['lemmas'].size,
                embed_size=args.output_lemma_embed_size,
                name='output_lemma_embedding')

            ## Need to compute U[pred_embeddings; role_embeddings] for
            ## every pair of predicate and role, but it's faster to do
            ## the multiplications separately and then add the results
            Up = tf.get_variable(
                'Up',
                shape=(args.output_lemma_embed_size, lstm_output_size),
                initializer=tf.orthogonal_initializer(),
                dtype=tf.float32)
            Wp = tf.matmul(pred_embeddings, Up)

            Ur = tf.get_variable(
                'Ur',
                shape=(args.role_embed_size, lstm_output_size),
                initializer=tf.orthogonal_initializer(),
                dtype=tf.float32)
            Wr = tf.matmul(role_embeddings, Ur)

            ## Tile the results so that we can add them, plus a bias term
            ## W: (batch_size,  lstm_output_size, num_roles)
            Wp_tiled = tf.tile(tf.expand_dims(Wp, 1), (1, num_roles, 1))
            Wr_tiled = tf.tile(tf.expand_dims(Wr, 0), (batch_size, 1, 1))
            b = tf.get_variable(
                'b',
                shape=(lstm_output_size,),
                initializer=tf.constant_initializer(0.0),
                dtype=tf.float32)
            W = tf.transpose(tf.nn.relu(Wp_tiled + Wr_tiled + b),
                             perm=[0,2,1])
            
            # (batch_size, seq_len, out_size)*(batch_size, out_size, num_roles)
            # = (batch_size, seq_len, num_roles)
            logits = tf.matmul(combined_outputs, W)

            # Mask the roles that can't be assigned (given the predicate).
            # labels_mask_placeholder is shaped (batch_size, num_roles),
            # so tile the masks and multiply elementwise.
            if args.restrict_labels:
                masks = tf.tile(tf.expand_dims(labels_mask_placeholder, 1),
                                (1, seq_length, 1))
                logits = tf.multiply(logits, masks)

            predictions = tf.nn.softmax(logits)
                

        # Loss op and optimizer
        cross_ent = tf.nn.sparse_softmax_cross_entropy_with_logits(
            labels=labels_placeholder,
            logits=logits)
        loss = tf.reduce_mean(cross_ent)


        if args.optimizer == 'adadelta':
            optimizer = tf.train.AdadeltaOptimizer()
        else:
            optimizer = tf.train.AdamOptimizer()


        ## compute_gradients prints some of the split gradients to stdout
        ## for whatever reason, so capture that here
        redirect = Redirect()
        sys.stdout = redirect
        gvs = optimizer.compute_gradients(loss)
        sys.stdout = redirect.stdout

        ## Clip gradients (https://stackoverflow.com/a/36501922)        
        clipped_gvs = [(tf.clip_by_value(grad, -1., 1.), var)
                       for grad, var in gvs]
        train_op = optimizer.apply_gradients(clipped_gvs)


        # Add everything to the model
        self.words_placeholder = words_placeholder
        self.freqs_placeholder = freqs_placeholder
        self.pos_placeholder = pos_placeholder
        self.lemmas_placeholder = lemmas_placeholder
        self.preds_placeholder = preds_placeholder
        self.preds_idx_placeholder = preds_idx_placeholder
        self.labels_placeholder = labels_placeholder
        self.labels_mask_placeholder = labels_mask_placeholder
        self.stags_placeholder = stags_placeholder
        self.use_dropout_placeholder = use_dropout_placeholder
        self.seq_lengths_placeholder = seq_lengths_placeholder
        self.predictions = predictions
        self.loss = loss
        self.train_op = train_op

        self.training_batches = None
        self.testing_batches = None
        self.elmo_placeholder = elmo_placeholder


    def batch_to_feed(self, batch):
        (elmo, words, freqs, pos, lemmas, preds, preds_idx,
         labels, labels_mask, stags, seq_lengths) = batch
        feed_dict = {
            self.elmo_placeholder: elmo,
            self.words_placeholder: words,
            self.freqs_placeholder: freqs,
            self.pos_placeholder: pos,
            self.lemmas_placeholder: lemmas,
            self.preds_placeholder: preds,
            self.preds_idx_placeholder: preds_idx,
            self.labels_placeholder: labels,
            self.labels_mask_placeholder: labels_mask,
            self.stags_placeholder: stags,
            self.seq_lengths_placeholder: seq_lengths
        }
        return feed_dict
        

    def run_training_batch(self, session, batch):
        """
        A batch contains input tensors for words, pos, lemmas, preds,
          preds_idx, and labels (in that order)
        Runs the model on the batch (through train_op if train=True)
        Returns the loss
        """
        feed_dict = self.batch_to_feed(batch)
        feed_dict[self.use_dropout_placeholder] = 1.0
        fetches = [self.loss, self.train_op]

        # options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
        # run_metadata = tf.RunMetadata()
        
        loss, _ = session.run(fetches, feed_dict=feed_dict)
        # loss, _ = session.run(fetches,
        #                       feed_dict=feed_dict,
        #                       options=options,
        #                       run_metadata=run_metadata)
        
        # fetched_timeline = timeline.Timeline(run_metadata.step_stats)
        # chrome_trace = fetched_timeline.generate_chrome_trace_format()
        # with open('timeline.json', 'w') as f:
        #     f.write(chrome_trace)
        
        return loss


    def run_testing_batch(self, session, batch):
        """
        A batch contains input tensors for words, pos, lemmas, preds,
          preds_idx, and labels (in that order)
        Runs the model on the batch (through train_op if train=True)
        Returns loss and also predicted argument labels.
        """
        feed_dict = self.batch_to_feed(batch)
        feed_dict[self.use_dropout_placeholder] = 0.0
        fetches = [self.loss, self.predictions]
        loss, probabilities = session.run(fetches, feed_dict=feed_dict)
        return loss, probabilities
    

    def run_training_epoch(self, session, vocabs, fn_txt, fn_preds, fn_stags,
                           language):
        batch_size = self.args.batch_size
        total_loss = 0
        num_batches = 0

        if self.training_batches is None:
            print('Loading training batches...')
            self.training_batches = [batch for batch in batch_producer(
                batch_size, vocabs, fn_txt, fn_preds, fn_stags,
                language, train=True)]
            print('Loaded {} training batches'.format(
                len(self.training_batches)))
        total_batches = len(self.training_batches)
        
        for i, (_, batch) in enumerate(self.training_batches):
            loss = self.run_training_batch(session, batch)
            total_loss += loss
            num_batches += 1
            if i % 10 == 0:
                avg_loss = total_loss / num_batches
                batch_size = len(batch[0][1])
                msg = '\r{}/{}    loss: {}    batch_size: {}'.format(
                    i, total_batches, avg_loss, batch_size)
                sys.stdout.write(msg)
                sys.stdout.flush()
        print('\n')

        return total_loss / num_batches


    def run_testing_epoch(self, session, vocabs, fn_txt, fn_preds,
                          fn_stags, fn_sys, language):
        batch_size = self.args.batch_size
        total_loss = 0
        num_batches = 0

        if self.testing_batches is None:
            print('Loading testing batches...')
            self.testing_batches = [batch for batch in batch_producer(
                batch_size, vocabs, fn_txt, fn_preds, fn_stags,
                language, train=False)]
            print('Loaded {} testing batches.'.format(
                len(self.testing_batches)))
        total_batches = len(self.testing_batches)
        
        predicted_sents = []
        for i, (sents, batch) in enumerate(self.testing_batches):
            batch_loss, probabilities = self.run_testing_batch(session, batch)
            total_loss += batch_loss
            num_batches += 1

            # Add the predictions to the sentence objects for later evaluation
            for sent, probs in zip(sents, probabilities):
                sent.add_predictions(probs, vocabs['labels'],
                                     restrict_labels=self.args.restrict_labels)
                # (sent.parent is the complete sentence, as opposed to the
                # predicate-specific sentence)
                if sent.parent not in predicted_sents:
                    predicted_sents.append(sent.parent)
            
            if i % 10 == 0:
                avg_loss = total_loss / num_batches
                msg = '\r{}/{}    loss: {}'.format(
                    i, total_batches, avg_loss)
                sys.stdout.write(msg)
                sys.stdout.flush()
        print('\n')
        self.test_batches = num_batches

        # Write the predictions to a file for evaluation
        with open(fn_sys, 'w') as f:
            for sent in predicted_sents:
                f.write(str(sent) + '\n')
        print('Wrote predictions to', fn_sys)
    
        return total_loss / num_batches    
