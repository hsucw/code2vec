#!/usr/bin/env python3

import tensorflow as tf
import sys
import os
import numpy as np
import bf
import pwn
import collections
import argparse

def parse_arguments():
    parser=argparse.ArgumentParser()
    parser.add_argument("hashes", help="the hash list", type=str)
    parser.add_argument("bloom", help="the bloom filter pickle file", type=str)
    parser.add_argument("model", help="the output trained model", type=str)
    parser.add_argument("-k",
	help="the total number of hash functions (def:7)",
        type=int,
        default=7)
    parser.add_argument("-max_bf_size", "-bf",
        help="the max index of hash functions (def:2^16)",
        type=int,
        default=65535)
    parser.add_argument("-noc",
        help="the number of most common (def:50000)",
        type=int,
        default=50000)
    parser.add_argument("-emb",
        help="the embedding size (def:128)",
        type=int,
        default=128)
    parser.add_argument("-top",
        help="show top n nearnest words",
        type=int,
        default=10)
    return parser.parse_args()

args = parse_arguments()
print("args config:{}".format(args))

#if len(sys.argv) != 4:
#    print('Usage:\n\tsimilarity.py <input filename> <bloom filter plk> <model filename>')
#    sys.exit(-1)
n_words = args.noc
embedding_size = args.emb # Dimension of the embedding vector.
top_k = args.top

print('Loading bloomfilter...', end='')
bloomfilter = bf.bloomfilter()
bloomfilter.load(args.bloom)
max_bf_size = bloomfilter.size
print('Done')


def get_word_indice(word):
    indice = bloomfilter.get_indice(word)
    return indice


def read_data(filename, n_words):
    """Extract the first file enclosed in a zip file as a list of words."""
    with open(filename) as f:
        filter_set = set()
        unsorted_res = []
        words = []
        count = []
        for line in f:
            word = line.strip()
            if len(word) == 0:
                continue
            word_idx_list = [int(idx) for idx in word.split(',')]
            filter_set.add(tuple(word_idx_list))
            words.append(tuple(sorted(word_idx_list)))
        words_counter = collections.Counter(words)
        most_common_words = dict()
        most_common_words_counter = words_counter.most_common(n_words)
        for item in most_common_words_counter:
            most_common_words[item[0]] = True

        for w in filter_set:
            if tuple(sorted(list(w))) in most_common_words:
                unsorted_res.append(list(w))

    del most_common_words
    del words
    del count
    del filter_set

    return unsorted_res


print('Read vocabulary from {}...'.format(args.hashes), end='')
vocabulary = read_data(args.hashes, n_words)
print('Done')

num_hash_fun = bloomfilter.k

print('Construct required tf graph...', end='')

graph = tf.Graph()

with graph.as_default():
    test_input = tf.placeholder(tf.int32, shape=[num_hash_fun])

    # Ops and variables pinned to the CPU because of missing GPU implementation
    with tf.device('/cpu:0'):
        embeddings = tf.Variable(
            tf.random_uniform([max_bf_size, embedding_size], -1.0, 1.0))

    # Compute the average NCE loss for the batch.
    # tf.nce_loss automatically draws a new sample of the negative labels each
    # time we evaluate the loss.
    test_vec = tf.nn.embedding_lookup(embeddings, test_input)
    test_vec = tf.reduce_mean(test_vec, 0)
    test_vec = tf.expand_dims(test_vec, 0)
    print('test_vec.shape: ', test_vec.shape)


    # Compute the cosine similarity between minibatch examples and all embeddings.
    all_words_embeddings = tf.nn.embedding_lookup(embeddings, vocabulary)
    all_words_embeddings = tf.reduce_mean(all_words_embeddings, 1)
    norm = tf.sqrt(tf.reduce_sum(tf.square(all_words_embeddings), 1, keep_dims=True))
    all_words_embeddings = all_words_embeddings / norm

    similarity = tf.matmul(test_vec, all_words_embeddings, transpose_b=True) / tf.sqrt(tf.reduce_sum(tf.square(test_vec)))

    # Add variable initializer.
    init = tf.global_variables_initializer()

print('Done')
print('Tensorflow session start')

with tf.Session(graph=graph) as session:
    # We must initialize all variables before we use them.
    init.run()

    print('Restore embeddings weights from model({})...'.format(args.model), end='')
    saver = tf.train.Saver({'embeddings': embeddings})
    saver.restore(session, args.model)
    print('Done')

    while True:
        raw_user_input = input('Please input a word (idx1,idx2,...idx7) or word or exit:')
        if raw_user_input == 'exit':
            break
        if raw_user_input.startswith('('):
            user_input = [int(i) for i in raw_user_input[1:-1].split(',')]
        else:
            user_input = get_word_indice(raw_user_input)

        feed_dict = {test_input: user_input}
        sim = session.run([similarity], feed_dict=feed_dict)[0][0]
        distance = np.sort((-sim))[0:top_k + 1]
        nearest = (-sim).argsort()[0:top_k + 1]
        log_str = 'Nearest to <{}>:'.format(raw_user_input)
        for k in range(top_k): # Iterate each top_k closed word
            close_opcode_indice = vocabulary[nearest[k]] # all the hash value of the closed word
            opcode_str = ''
            possible_words = set()
            for idx, val in enumerate(close_opcode_indice):
                if idx == 0:
                    possible_words = bloomfilter.get_opcode_in_table(idx, val)
                else:
                    possible_words = possible_words & bloomfilter.get_opcode_in_table(idx, val)
            # opcode_asm = pwn.disasm(opcode)
            if len(possible_words) == 0:
                print('Unable to find reversed opcode for: {}'.format(close_opcode_indice))
            else:
                print('{}\t{}'.format(distance[k], possible_words))
        print('=' * 80)
