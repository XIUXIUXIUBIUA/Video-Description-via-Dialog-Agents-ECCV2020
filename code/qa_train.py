#!/usr/bin/env python
"""Train Q-bot to summarize a video
"""

import argparse
import logging
import math
import sys
import time
import random
import os
import json

import numpy as np
import pickle
import six
import threading

import torch

import qa_data_handler as dh

from new_qa_bot_model import MMSeq2SeqModel
from lstm_encoder import LSTMEncoder
from hlstm_encoder import HLSTMEncoder
from hlstm_decoder import HLSTMDecoder
from summary_decoder import Summary_HLSTMDecoder
from question_decoder import Question_HLSTMDecoder

def initialize_model_weights(model, initialization, lstm_initialization):
    if initialization == "he":
        print("kaiming normal initialization.")
    elif initialization == "xavier":
        print("xavier normal initialization.")
    else:
        print("default initialization, no changes made.")
    if(initialization):
        for name, param in model.named_parameters():
            print("\n"+name)

            # Bias params
            if("bias" in name.split(".")[-1]):
                print("zero")
                param.data.zero_()

            # Batchnorm weight params
            elif("weight" in name.split(".")[-1] and len(param.size())==1):
                print("batchnorm weight: default initialization")

            # LSTM weight params
            elif("weight" in name.split(".")[-1] and "lstm" in name):
                if "xavier" in lstm_initialization:
                    print("xavier")
                    torch.nn.init.xavier_normal(param)
                elif "he" in lstm_initialization:
                    print("he")
                    torch.nn.init.kaiming_normal(param)

            # Other weight params
            elif("weight" in name.split(".")[-1] and "lstm" not in name):
                if "xavier" in initialization:
                    print("xavier")
                    torch.nn.init.xavier_normal(param)
                elif "he" in initialization:
                    print("he")
                    torch.nn.init.kaiming_normal(param)

def fetch_batch_a(dh, data, index, result):
    result.append(dh.make_batch_a(data, index))

def fetch_batch_q(dh, data, index, result):
    result.append(dh.make_batch_q(data, index))

# Evaluation routine
def evaluate(model, data, indices):
    start_time = time.time()
    eval_loss = 0.
    eval_num_words = 0
    model.eval()
    with torch.no_grad():
        # fetch the first batch
        batch_a = [dh.make_batch_a(data, indices[0])]
        batch_q = [dh.make_batch_q(data, indices[0])]
        # evaluation loop
        for j in six.moves.range(len(indices)):
            # get a fetched batch
            x_batch, h_batch, q_batch, a_batch_in, a_batch_out, s_batch, summary_batch_in, summary_batch_out, c_batch = batch_a.pop()
            q_batch_in, q_batch_out, all_a_batch_in, all_q_batch_in = batch_q.pop()
            # fetch the next batch in parallel
            if j < len(indices) - 1:
                prefetch1 = threading.Thread(target=fetch_batch_a,
                                            args=([dh, data, indices[j + 1], batch_a]))
                prefetch2 = threading.Thread(target=fetch_batch_q,
                                            args=([dh, data, indices[j + 1], batch_q]))
                prefetch1.start()
                prefetch2.start()
            # propagate for training
            if len(h_batch) < 12:
                x = [torch.from_numpy(x) for x in x_batch]
                h = [[torch.from_numpy(h) for h in hb] for hb in h_batch]
                q = [torch.from_numpy(q) for q in q_batch]
                ai = [torch.from_numpy(ai) for ai in a_batch_in]
                ao = [torch.from_numpy(ao) for ao in a_batch_out]
                s = torch.from_numpy(s_batch).cuda().float()
                smi = [torch.from_numpy(smi) for smi in summary_batch_in] 
                smo = [torch.from_numpy(smo) for smo in summary_batch_out]
                c = [torch.from_numpy(c) for c in c_batch]
                qi = [torch.from_numpy(qi) for qi in q_batch_in]
                qo = [torch.from_numpy(qo) for qo in q_batch_out]
                all_ai = [torch.from_numpy(all_ai) for all_ai in all_a_batch_in]
                all_qi = [torch.from_numpy(all_qi) for all_qi in all_q_batch_in]

                _, _, loss = model.loss(x, h, q, c, ai, qi, smi, ao, qo, smo, s, all_ai, all_qi)

                num_words = sum([len(s) for s in smo])
                eval_loss += loss.cpu().data.numpy() * num_words
                eval_num_words += num_words
                # wait prefetch completion
            prefetch1.join()
            prefetch2.join()
    model.train()

    wall_time = time.time() - start_time
    return math.exp(eval_loss / eval_num_words), wall_time

class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


##################################
# main
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', '-g', default=0, type=int,
                        help='GPU ID (negative value indicates CPU)')
    # train, dev and test data
    parser.add_argument('--vocabfile', default='', type=str,
                        help='Vocabulary file (.json)')
    parser.add_argument('--dictmap', default='', type=str,
                        help='Dict id-map file (.json)')
    parser.add_argument('--fea-type', nargs='+', type=str,
                        help='Image feature files (.pkl)')
    parser.add_argument('--train-path', default='', type=str,
                        help='Path to training feature files')
    parser.add_argument('--train-set', default='', type=str,
                        help='Filename of train data')
    parser.add_argument('--valid-path', default='', type=str,
                        help='Path to validation feature files')
    parser.add_argument('--valid-set', default='', type=str,
                        help='Filename of validation data')
    parser.add_argument('--include-caption', action='store_true',
                        help='Include caption in the history')
    # Attention model related
    parser.add_argument('--model', '-m', default='', type=str,
                        help='Attention model to be output')
    parser.add_argument('--num-epochs', '-e', default=15, type=int,
                        help='Number of epochs')
    # multimodal encoder parameters
    parser.add_argument('--enc-psize', '-p', nargs='+', type=int,
                        help='Number of projection layer units')
    parser.add_argument('--enc-hsize', '-u', nargs='+', type=int,
                        help='Number of hidden units')
    parser.add_argument('--att-size', '-a', default=100, type=int,
                        help='Number of attention layer units')
    parser.add_argument('--mout-size', default=100, type=int,
                        help='Number of output layer units')
    # input (question) encoder parameters
    parser.add_argument('--embed-size', default=200, type=int,
                        help='Word embedding size')
    parser.add_argument('--in-enc-layers', default=2, type=int,
                        help='Number of input encoder layers')
    parser.add_argument('--in-enc-hsize', default=200, type=int,
                        help='Number of input encoder hidden layer units')
    # history (QA pairs) encoder parameters
    parser.add_argument('--hist-enc-layers', nargs='+', type=int,
                        help='Number of history encoder layers')
    parser.add_argument('--hist-enc-hsize', default=200, type=int,
                        help='History embedding size')
    parser.add_argument('--hist-out-size', default=200, type=int,
                        help='History embedding size')
    # response (answer) decoder parameters
    parser.add_argument('--dec-layers', default=2, type=int,
                        help='Number of decoder layers')
    parser.add_argument('--dec-psize', '-P', default=200, type=int,
                        help='Number of decoder projection layer units')
    parser.add_argument('--dec-hsize', '-d', default=200, type=int,
                        help='Number of decoder hidden layer units')
    # Training conditions
    parser.add_argument('--optimizer', '-o', default='AdaDelta', type=str,
                        choices=['SGD', 'Adam', 'AdaDelta', 'RMSprop'],
                        help="optimizer")
    parser.add_argument('--rand-seed', '-s', default=1, type=int,
                        help="seed for generating random numbers")
    parser.add_argument('--batch-size', '-b', default=20, type=int,
                        help='Batch size in training')
    parser.add_argument('--max-length', default=20, type=int,
                        help='Maximum length for controling batch size')
    # others
    parser.add_argument('--verbose', '-v', default=0, type=int,
                        help='verbose level')
    parser.add_argument('--model_name', help='Name of the model')

    args = parser.parse_args()
    random.seed(args.rand_seed)
    np.random.seed(args.rand_seed)

    if args.dictmap != '':
        dictmap = json.load(open(args.dictmap, 'r'))
    else:
        dictmap = None

    if args.verbose >= 1:
        logging.basicConfig(level=logging.DEBUG,
                            format='%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s')
    else:
        logging.basicConfig(level=logging.INFO,
                            format='%(asctime)s %(levelname)s: %(message)s')

    logging.info('Command line: ' + ' '.join(sys.argv))
    # get vocabulary
    logging.info('Extracting words from ' + args.train_set)
    vocab = dh.get_vocabulary(args.train_set, include_caption=args.include_caption)
    # load data
    logging.info('Loading training data from ' + args.train_set)
    train_data = dh.load(args.fea_type, args.train_path, args.train_set,
                         vocabfile=args.vocabfile,
                         include_caption=args.include_caption,
                         vocab=vocab, dictmap=dictmap)

    logging.info('Loading validation data from ' + args.valid_set)
    valid_data = dh.load(args.fea_type, args.valid_path, args.valid_set,
                         vocabfile=args.vocabfile,
                         include_caption=args.include_caption,
                         vocab=vocab, dictmap=dictmap)

    feature_dims, spatial_dims = dh.feature_shape(train_data)
    logging.info("Detected feature dims: {}".format(feature_dims));

    # Prepare RNN model and load data
    #embed_model = nn.Embedding(len(vocab), args.embed_size)
    #print("vocab:", len(vocab))
    embed_model = None
    dropout = 0.5
    model = MMSeq2SeqModel(
        None,
        HLSTMEncoder(args.hist_enc_layers[0], args.hist_enc_layers[1],
                     len(vocab), args.hist_out_size, args.embed_size,
                     args.hist_enc_hsize, dropout=dropout, embed=embed_model),
        LSTMEncoder(args.in_enc_layers, len(vocab), args.in_enc_hsize,
                    args.embed_size, dropout=dropout, embed=embed_model),
        LSTMEncoder(args.in_enc_layers, len(vocab), args.in_enc_hsize,
                    args.embed_size, dropout=dropout, embed=embed_model),
        HLSTMDecoder(args.dec_layers, len(vocab), len(vocab), args.embed_size,
                    args.hist_out_size + args.in_enc_hsize,
                     args.dec_hsize, args.dec_psize,
                     independent=False, dropout=dropout, embed=embed_model),
        Summary_HLSTMDecoder(args.dec_layers, len(vocab), len(vocab), args.embed_size,
                    args.hist_out_size + args.in_enc_hsize,
                     args.dec_hsize, args.dec_psize,
                     independent=False, dropout=dropout, embed=embed_model),
        Question_HLSTMDecoder(args.dec_layers, len(vocab), len(vocab), args.embed_size,
                    args.hist_out_size + args.in_enc_hsize,
                     args.dec_hsize, args.dec_psize,
                     independent=False, dropout=dropout, embed=embed_model),
        )

    # check param number
    print('Param number:', sum(param.numel() for param in model.parameters()))
    initialize_model_weights(model, "he", "xavier")
    # report data summary
    logging.info('#vocab = %d' % len(vocab))
    # make batchset for training
    logging.info('Making mini batches for training data')
    train_indices, train_samples = dh.make_batch_indices(train_data, args.batch_size,
                                                         max_length=args.max_length)
    logging.info('#train sample = %d' % train_samples)
    logging.info('#train batch = %d' % len(train_indices))
    # make batchset for validation
    logging.info('Making mini batches for validation data')
    valid_indices, valid_samples = dh.make_batch_indices(valid_data, args.batch_size,
                                                         max_length=args.max_length)
    logging.info('#validation sample = %d' % valid_samples)
    logging.info('#validation batch = %d' % len(valid_indices))
    # copy model to gpu
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model.to(device)
    # save meta parameters
    path = args.model + '.conf'
    with open(path, 'wb') as f:
        pickle.dump((vocab, args), f, -1)

    # start training
    logging.info('----------------')
    logging.info('Start training')
    logging.info('----------------')
    # Setup optimizer
    if args.optimizer == 'SGD':
        optimizer = torch.optim.SGD(model.parameters())
    elif args.optimizer == 'Adam':
        optimizer = torch.optim.Adam(model.parameters())
    elif args.optimizer == 'AdaDelta':
        optimizer = torch.optim.Adadelta(model.parameters())
    elif args.optimizer == 'RMSprop':
        optimizer = torch.optim.RMSprop(model.parameters())

    # initialize status parameters
    modelext = '.pth.tar'
    cur_loss = 0.
    cur_num_words = 0
    epoch = 0
    start_at = time.time()
    cur_at = start_at
    min_valid_ppl = 1.0e+10
    n = 0
    report_interval = 1000 / args.batch_size
    bestmodel_num = 0

    random.shuffle(train_indices)
    # do training iterations
    for i in six.moves.range(args.num_epochs):
        logging.info('Epoch %d : %s' % (i + 1, args.optimizer))
        train_loss = 0.
        train_num_words = 0
        batch_time = AverageMeter()
        data_time = AverageMeter()
        end = time.time()
        # fetch the first batch
        batch_a = [dh.make_batch_a(train_data, train_indices[0])]
        batch_q = [dh.make_batch_q(train_data, train_indices[0])]
        #test_count = 0
        # train iterations
        count = 0
        cul_loss_batch = 0
        for j in six.moves.range(len(train_indices)):
            data_time.update(time.time() - end)
            # get fetched batch
            x_batch, h_batch, q_batch, a_batch_in, a_batch_out, s_batch, summary_batch_in, summary_batch_out, c_batch = batch_a.pop()
            q_batch_in, q_batch_out, all_a_batch_in, all_q_batch_in = batch_q.pop()
            # fetch the next batch in parallel

            if j < len(train_indices) - 1:
                prefetch1 = threading.Thread(target=fetch_batch_a,
                                            args=([dh, train_data, train_indices[j + 1], batch_a]))
                prefetch2 = threading.Thread(target=fetch_batch_q,
                                            args=([dh, train_data, train_indices[j + 1], batch_q]))
                prefetch1.start()
                prefetch2.start()



            # propagate for training
            # x is audio, list
            x = [torch.from_numpy(x) for x in x_batch]


            h = [[torch.from_numpy(h) for h in hb] for hb in h_batch]
            q = [torch.from_numpy(q) for q in q_batch]
            c = [torch.from_numpy(c) for c in c_batch]
            ai = [torch.from_numpy(ai) for ai in a_batch_in]
            ao = [torch.from_numpy(ao) for ao in a_batch_out]

            qi = [torch.from_numpy(qi) for qi in q_batch_in]
            qo = [torch.from_numpy(qo) for qo in q_batch_out]            

            ###summary
            smi = [torch.from_numpy(smi) for smi in summary_batch_in]
            smo = [torch.from_numpy(smo) for smo in summary_batch_out]

            all_ai = [torch.from_numpy(all_ai) for all_ai in all_a_batch_in]
            all_qi = [torch.from_numpy(all_qi) for all_qi in all_q_batch_in]

            s = torch.from_numpy(s_batch).cuda().float()
            if len(h_batch) < 12:
                _, _, loss = model.loss(x, h, q, c, ai, qi, smi, ao, qo, smo, s, all_ai, all_qi)

                num_words = sum([len(s) for s in smo])
                batch_loss = loss.cpu().data.numpy()
                train_loss += batch_loss * num_words
                train_num_words += num_words

                cur_loss += batch_loss * num_words
                cur_num_words += num_words
                if (n + 1) % report_interval == 0:
                    now = time.time()
                    throuput = report_interval / (now - cur_at)
                    perp = math.exp(cur_loss / cur_num_words)
                    logging.info('iter {}, '
                                 'time {:.3f} ({:.3f})\t'
                                 'data {:.3f} ({:.3f})\t'
                                 'training perplexity: {:.2f} ({:.2f} iters/sec)'
                                 .format(n + 1, batch_time.val, batch_time.avg,
                                         data_time.val, data_time.avg, perp, throuput))

                    cur_at = now
                    cur_loss = 0.
                    cur_num_words = 0
                n += 1

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                batch_time.update(time.time() - end)
                end = time.time()




            # wait prefetch completion
            prefetch1.join()
            prefetch2.join()


        logging.info("epoch: %d  train perplexity: %f" % (i + 1, math.exp(train_loss / train_num_words)))
        # validation step
        logging.info('-----------------------validation--------------------------')
        now = time.time()
        valid_ppl, valid_time = evaluate(model, valid_data, valid_indices)
        #valid_ppl  = 0
        #valid_time = 0 
        logging.info('validation perplexity: %.4f' % (valid_ppl))

        # update the model via comparing with the lowest perplexity
        modelfile = args.model + '_' + str(i + 1) + modelext
        logging.info('writing model params to ' + modelfile)
        torch.save(model, modelfile)

        if min_valid_ppl > valid_ppl:
            bestmodel_num = i + 1
            logging.info('validation perplexity reduced %.4f -> %.4f' % (min_valid_ppl, valid_ppl))
            min_valid_ppl = valid_ppl

        cur_at += time.time() - now  # skip time of evaluation and file I/O
        logging.info('----------------')

    # make a symlink to the best model
    logging.info('the best model is epoch %d.' % bestmodel_num)
    logging.info('a symbolic link is made as ' + args.model + '_best' + modelext)
    if os.path.exists(args.model + '_best' + modelext):
        os.remove(args.model + '_best' + modelext)
    os.symlink(os.path.basename(args.model + '_' + str(bestmodel_num) + modelext),
               args.model + '_best' + modelext)
    logging.info('done')
