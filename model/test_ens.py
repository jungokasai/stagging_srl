# Test a trained SRL model
from __future__ import print_function
from __future__ import division

import os
import argparse
import tensorflow as tf
import numpy as np
import cPickle as pickle

from model.srl_ens import SRL_Model_Ens
from eval.eval import run_evaluation_script
from util import vocab


parser = argparse.ArgumentParser()
parser.add_argument("model_dir", help="Directory containing the saved model")
parser.add_argument("data", help="train, test, dev, or ood",
                    choices=['train', 'test', 'dev', 'ood'])
parser.add_argument("-rl", "--restrict_labels", dest="restrict_labels",
                    help="Only allow valid labels",
                    action="store_true", default=False)


def test(args):
    model_dir = args.model_dir    
    with open(os.path.join(model_dir, 'args.pkl'), 'r') as f:
        model_args = pickle.load(f)
    if not hasattr(model_args, 'language'):
        model_args.language = 'eng'

    model_args.stags_dir = 'pred'
        
    fn_txt_valid = 'data/{}/conll09/{}.txt'.format(
        model_args.language, args.data)
    fn_preds_valid = 'data/{}/conll09/pred/{}_predicates.txt'.format(
        model_args.language, args.data)
    fn_stags_valid = 'data/{}/conll09/{}/{}_stags_{}.txt'.format(
        model_args.language, model_args.stags_dir, args.data, model_args.stag_type)
    fn_sys = 'output/predictions/{}.txt'.format(args.data)
    
    vocabs = vocab.get_vocabs(model_args.language, model_args.stag_type)

    with tf.Graph().as_default():
        tf.set_random_seed(model_args.seed)
        np.random.seed(model_args.seed)    
    
        print("Building model...")
        model = SRL_Model_Ens(vocabs, model_args)

        saver = tf.train.Saver()

        with tf.Session() as session:
            print('Restoring model...')
            saver.restore(session, tf.train.latest_checkpoint(model_dir))

            print('-' * 78)
            print('Validating...')
            valid_loss = model.run_testing_epoch(
                session, vocabs, fn_txt_valid, fn_preds_valid,
                fn_stags_valid, fn_sys, model_args.language)
            print('Validation loss: {}'.format(valid_loss))

            print('-' * 78)
            print('Running evaluation script...')
            labeled_f1, unlabeled_f1 = run_evaluation_script(
                fn_txt_valid, fn_sys)
            print('Labeled F1:    {0:.2f}'.format(labeled_f1))
            print('Unlabeled F1:  {0:.2f}'.format(unlabeled_f1))


if __name__ == '__main__':
    args = parser.parse_args()
    test(args)
