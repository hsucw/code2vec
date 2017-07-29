#!/usr/bin/env python3
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import math
import os
import sys
import random
import zipfile
from bf import bloomfilter

import numpy as np
from six.moves import urllib
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf
import argparse

def parse_arguments():
    parser=argparse.ArgumentParser()
    parser.add_argument("input", help="the input, a hash list", type=str)
    parser.add_argument("output", help="the output, a trained model", type=str)
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
    parser.add_argument("-bat",
        help="the batch size (def:130)",
        type=int,
        default=130)
    parser.add_argument("-emb",
        help="the embedding size (def:128)",
        type=int,
        default=128)
    parser.add_argument("-sw",
        help="the skip window, consider left and right (def:1)",
        type=int,
        default=1)
    parser.add_argument("-ns",
        help="the toal number of skip words (def:2, <= 2*num_args.sw)",
        type=int,
        default=2)
    parser.add_argument("-neg",
        help="the total number of negative samples (def:64)",
        type=int,
        default=64)
    parser.add_argument("-top",
        help="show top n nearnest words",
        type=int,
        default=10)
    parser.add_argument("-epoch",
        help="the total number of training epoch",
        type=int,
        default=100000)
    return parser.parse_args()

args = parse_arguments()
print("args config:{}".format(args))

def read_data(filename):
    """
        Read all hashes from a file, splited by comma.
    """
    with open(filename) as f:
        res = []
        for line in f:
            word = line.strip()
            if len(word) == 0:
                continue
            hashes= word.split(',')

            # We need to check the total number of hashes
            try:
                assert len(hashes) == args.k
            except AssertionError:
                print("The total number of hashes is not equal args.k={} != input.k={}".format(
                    args.k, len(hashes)))
                print("Use -k to identify a correct hash number k (should be \"-k {}\")".format(
                    len(hashes)))
                sys.exit(1)

            word_idx_list = [int(idx) for idx in hashes]
            res.append(tuple(sorted(word_idx_list)))
    return res

# Step 1: Read hash lists
vocabulary = read_data(args.input)
print('Data size', len(vocabulary))

# Step 1.5: construct the 'UNK' tuple
# [FIXIT] we do not check the 'UNK's max_bf_size and k
#         It will be wrong if they do not match with the input's
bloom_filter = bloomfilter(name="UNK", size=args.max_bf_size, k=args.k)
unknow_indice = tuple(sorted(bloom_filter.get_indice('UNK')))

# Step 2: Build the dictionary and replace rare words with UNK token.
def build_dataset(words, n_words):
    """Process raw inputs into a dataset."""

    # [FIXIT] Current 'UNK' is the most frequent word.
    #         But what if not?
    count = [[unknow_indice, -1]]
    rank_matrix = []
    words_counter = collections.Counter(words)
    count.extend(words_counter.most_common(n_words))
    dictionary = dict()
    for word, _ in count:
        dictionary[word] = len(dictionary)

    data = list()
    unk_count = 0
    for word in words:
        if word in dictionary:
            indice = word
        else:
            indice = unknow_indice  # dictionary['UNK']
            unk_count += 1
        data.append(indice)
    count[0][1] = unk_count
    count[0] = tuple(count[0])

    reversed_dictionary = dict(zip(dictionary.values(), dictionary.keys()))
    for i in range(len(count)):
        rank_matrix.append(list(reversed_dictionary[i]))
    return data, count, dictionary, reversed_dictionary, rank_matrix


vocabulary, count, dictionary, reverse_dictionary, rank_matrix = build_dataset(vocabulary, args.noc)
vocabulary_size = len(dictionary)

print('Most common words (+UNK)', count[:5])
data_index = 0

# Step 3: Function to generate a training batch for the skip-gram model.

vocabulary = [list(v) for v in vocabulary]
print('len(vocabulary):', len(vocabulary))

def generate_batch(batch_size, num_skips, skip_window):
    global data_index
    assert batch_size % num_skips == 0
    assert num_skips <= 2 * skip_window
    batch = np.ndarray(shape=(batch_size, args.k), dtype=np.int32)
    labels = np.ndarray(shape=(batch_size, 1), dtype=np.int32)
    span = 2 * skip_window + 1  # [ skip_window target skip_window ]
    buffer = collections.deque(maxlen=span)
    for _ in range(span):
        buffer.append(vocabulary[data_index])
        data_index = (data_index + 1) % len(vocabulary)
    for i in range(batch_size // num_skips):
        target = skip_window  # target label at the center of the buffer
        targets_to_avoid = [skip_window]
        for j in range(num_skips):
            while target in targets_to_avoid:
                target = random.randint(0, span - 1)
            targets_to_avoid.append(target)
            batch[i * num_skips + j] = buffer[skip_window]
            labels[i * num_skips + j, 0] = dictionary[tuple(buffer[target])]
        buffer.append(vocabulary[data_index])
        data_index = (data_index + 1) % len(vocabulary)
    # Backtrack a little bit to avoid skipping words in the end of a batch
    data_index = (data_index + len(vocabulary) - span) % len(vocabulary)
    return batch, labels


batch, labels = generate_batch(batch_size=args.bat,
        num_skips=args.ns,
        skip_window=args.sw)

#print(labels)
for i in range(8):
    print(batch[i], dictionary[tuple(batch[i])],
          '->', labels[i], reverse_dictionary[labels[i, 0]])

# Step 4: Build and train a skip-gram model.
# We pick a random validation set to sample nearest neighbors. Here we limit the
# validation samples to the words that have a low numeric ID, which by
# construction are also the most frequent.
valid_size = 16     # Random set of words to evaluate similarity on.
valid_window = 100  # Only pick dev samples in the head of the distribution.
valid_examples = np.random.choice(valid_window, valid_size, replace=False)

graph = tf.Graph()

with graph.as_default():

    # Input data.
    train_inputs = tf.placeholder(tf.int32, shape=[args.bat, args.k])
    train_labels = tf.placeholder(tf.int32, shape=[args.bat, 1])
    valid_dataset = tf.constant(valid_examples, dtype=tf.int32)
    rank_matrix = tf.stack(rank_matrix)

    # Ops and variables pinned to the CPU because of missing GPU implementation
    with tf.device('/cpu:0'):
        print('train_inputs.shape = ', train_inputs.shape)
        # Look up embeddings for inputs.
        embeddings = tf.Variable(
            tf.random_uniform([args.max_bf_size, args.emb], -1.0, 1.0))
        print('embeddings.shape = ', embeddings.shape)
        embed = tf.nn.embedding_lookup(embeddings, train_inputs)
        print('embed.shape = ', embed.shape)
        embed = tf.reduce_mean(embed, 1)
        print('embed.shape = ', embed.shape)



        # Construct the variables for the NCE loss
        nce_weights = tf.Variable(
            tf.truncated_normal([args.max_bf_size, args.emb],
                                stddev=1.0 / math.sqrt(args.emb)))
        nce_biases = tf.Variable(tf.zeros([args.max_bf_size]))

    # Compute the average NCE loss for the batch.
    # tf.nce_loss automatically draws a new sample of the negative labels each
    # time we evaluate the loss.

    loss = tf.reduce_mean(
        tf.nn.nce_loss(weights=nce_weights,
                       biases=nce_biases,
                       labels=train_labels,
                       inputs=embed,
                       num_sampled=args.neg,
                       num_classes=vocabulary_size,
                       rank_matrix=rank_matrix,
                       num_hash_func=args.k))

    # Construct the SGD optimizer using a learning rate of 1.0.
    optimizer = tf.train.GradientDescentOptimizer(1.0).minimize(loss)

    # Compute the cosine similarity between minibatch examples and all embeddings.
    norm = tf.sqrt(tf.reduce_sum(tf.square(embeddings), 1, keep_dims=True))
    normalized_embeddings = embeddings / norm
    valid_dataset_indice = tf.nn.embedding_lookup(rank_matrix, valid_dataset)
    valid_embeddings = tf.nn.embedding_lookup(
        normalized_embeddings, valid_dataset_indice)
    valid_embeddings = tf.reduce_mean(valid_embeddings, 1)

    all_words_embeddings = tf.nn.embedding_lookup(normalized_embeddings, rank_matrix)
    all_words_embeddings = tf.reduce_mean(all_words_embeddings, 1)

    similarity = tf.matmul(
        valid_embeddings, all_words_embeddings, transpose_b=True)
    print('similarity.shape = ', similarity.shape)
    # Add variable initializer.
    init = tf.global_variables_initializer()

# Step 5: Begin training.

with tf.Session(graph=graph) as session:
    # We must initialize all variables before we use them.
    init.run()
    print('Initialized')

    saver = tf.train.Saver({'embeddings': embeddings})

    average_loss = 0
    for step in xrange(args.epoch):
        batch_inputs, batch_labels = generate_batch(
            args.bat, args.ns, args.sw)
        feed_dict = {train_inputs: batch_inputs, train_labels: batch_labels}

        # We perform one update step by evaluating the optimizer op (including it
        # in the list of returned values for session.run()
        _, loss_val = session.run([optimizer, loss], feed_dict=feed_dict)
        # my_labels = session.run(train_labels, feed_dict=feed_dict)
        # print('feed_dict: {}'.format(feed_dict))
        # print('Vec: {}'.format(my_labels))
        average_loss += loss_val

        if step % 2000 == 0:
            if step > 0:
                average_loss /= 2000
            # The average loss is an estimate of the loss over the last 2000 batches.
            print('Average loss at step ', step, ': ', average_loss)
            average_loss = 0

        # Note that this is expensive (~20% slowdown if computed every 500 steps)
        '''
        if step % 10000 == 0:
            sim = similarity.eval()
            for i in xrange(valid_size):
                valid_word = reverse_dictionary[valid_examples[i]]
                top_k = 8  # number of nearest neighbors
                nearest = (-sim[i, :]).argsort()[1:top_k + 1]
                log_str = 'Nearest to {}: '.format(valid_word)
                for k in xrange(top_k):
                    close_word = reverse_dictionary[nearest[k]]
                    log_str = '%s %s,' % (log_str, close_word)
                print(log_str)
        '''

    final_embeddings = normalized_embeddings.eval()
    save_path = saver.save(session, args.output)

print('Training Finished!')
