#!/usr/bin/env python
# -*- coding: UTF-8 -*-
# File: CAM-resnet.py

import cv2
import sys
import argparse
import numpy as np
import os
import multiprocessing

import tensorflow as tf
from tensorflow.contrib.layers import variance_scaling_initializer
from tensorpack import *
from tensorpack.tfutils.symbolic_functions import *
from tensorpack.tfutils.summary import *

TOTAL_BATCH_SIZE = 256
INPUT_SHAPE = 224
DEPTH = None


class Model(ModelDesc):
    def _get_inputs(self):
        return [InputDesc(tf.uint8, [None, INPUT_SHAPE, INPUT_SHAPE, 3], 'input'),
                InputDesc(tf.int32, [None], 'label')]

    def _build_graph(self, inputs):
        image, label = inputs
        image = tf.cast(image, tf.float32) * (1.0 / 255)

        image_mean = tf.constant([0.485, 0.456, 0.406], dtype=tf.float32)
        image_std = tf.constant([0.229, 0.224, 0.225], dtype=tf.float32)
        image = (image - image_mean) / image_std
        image = tf.transpose(image, [0, 3, 1, 2])

        def shortcut(l, n_in, n_out, stride):
            if n_in != n_out:
                return Conv2D('convshortcut', l, n_out, 1, stride=stride)
            else:
                return l

        def basicblock(l, ch_out, stride, preact):
            ch_in = l.get_shape().as_list()[1]
            if preact == 'both_preact':
                l = BNReLU('preact', l)
                input = l
            elif preact != 'no_preact':
                input = l
                l = BNReLU('preact', l)
            else:
                input = l
            l = Conv2D('conv1', l, ch_out, 3, stride=stride, nl=BNReLU)
            l = Conv2D('conv2', l, ch_out, 3)
            return l + shortcut(input, ch_in, ch_out, stride)

        def bottleneck(l, ch_out, stride, preact):
            ch_in = l.get_shape().as_list()[1]
            if preact == 'both_preact':
                l = BNReLU('preact', l)
                input = l
            elif preact != 'no_preact':
                input = l
                l = BNReLU('preact', l)
            else:
                input = l
            l = Conv2D('conv1', l, ch_out, 1, nl=BNReLU)
            l = Conv2D('conv2', l, ch_out, 3, stride=stride, nl=BNReLU)
            l = Conv2D('conv3', l, ch_out * 4, 1)
            return l + shortcut(input, ch_in, ch_out * 4, stride)

        def layer(l, layername, block_func, features, count, stride, first=False):
            with tf.variable_scope(layername):
                with tf.variable_scope('block0'):
                    l = block_func(l, features, stride,
                                   'no_preact' if first else 'both_preact')
                for i in range(1, count):
                    with tf.variable_scope('block{}'.format(i)):
                        l = block_func(l, features, 1, 'default')
                return l

        cfg = {
            18: ([2, 2, 2, 2], basicblock),
            34: ([3, 4, 6, 3], basicblock),
            50: ([3, 4, 6, 3], bottleneck),
            101: ([3, 4, 23, 3], bottleneck)
        }
        defs, block_func = cfg[DEPTH]

        with argscope(Conv2D, nl=tf.identity, use_bias=False,
                      W_init=variance_scaling_initializer(mode='FAN_OUT')), \
                argscope([Conv2D, MaxPooling, GlobalAvgPooling, BatchNorm], data_format='NCHW'):
            convmaps = (LinearWrap(image)
                        .Conv2D('conv0', 64, 7, stride=2, nl=BNReLU)
                        .MaxPooling('pool0', shape=3, stride=2, padding='SAME')
                        .apply(layer, 'group0', block_func, 64, defs[0], 1, first=True)
                        .apply(layer, 'group1', block_func, 128, defs[1], 2)
                        .apply(layer, 'group2', block_func, 256, defs[2], 2)
                        .apply(layer, 'group3new', block_func, 512, defs[3], 1)
                        .BNReLU('bnlast')())
            print(convmaps)
            logits = (LinearWrap(convmaps)
                      .GlobalAvgPooling('gap')
                      .FullyConnected('linearnew', 1000, nl=tf.identity)())

        loss = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits, labels=label)
        loss = tf.reduce_mean(loss, name='xentropy-loss')

        wrong = prediction_incorrect(logits, label, 1, name='wrong-top1')
        add_moving_summary(tf.reduce_mean(wrong, name='train-error-top1'))

        wrong = prediction_incorrect(logits, label, 5, name='wrong-top5')
        add_moving_summary(tf.reduce_mean(wrong, name='train-error-top5'))

        wd_cost = regularize_cost('.*/W', l2_regularizer(1e-4), name='l2_regularize_loss')
        add_moving_summary(loss, wd_cost)
        self.cost = tf.add_n([loss, wd_cost], name='cost')

    def _get_optimizer(self):
        lr = get_scalar_var('learning_rate', 0.1, summary=True)
        opt = tf.train.MomentumOptimizer(lr, 0.9, use_nesterov=True)
        gradprocs = [gradproc.ScaleGradient(
            [('conv0.*', 0.1), ('group[0-2].*', 0.1)])]
        return optimizer.apply_grad_processors(opt, gradprocs)


def get_data(train_or_test):
    # completely copied from imagenet-resnet.py example
    isTrain = train_or_test == 'train'

    datadir = args.data
    ds = dataset.ILSVRC12(datadir, train_or_test,
                          shuffle=True if isTrain else False,
                          dir_structure='train')
    if isTrain:
        class Resize(imgaug.ImageAugmentor):
            def _augment(self, img, _):
                h, w = img.shape[:2]
                area = h * w
                for _ in range(10):
                    targetArea = self.rng.uniform(0.08, 1.0) * area
                    aspectR = self.rng.uniform(0.75, 1.333)
                    ww = int(np.sqrt(targetArea * aspectR))
                    hh = int(np.sqrt(targetArea / aspectR))
                    if self.rng.uniform() < 0.5:
                        ww, hh = hh, ww
                    if hh <= h and ww <= w:
                        x1 = 0 if w == ww else self.rng.randint(0, w - ww)
                        y1 = 0 if h == hh else self.rng.randint(0, h - hh)
                        out = img[y1:y1 + hh, x1:x1 + ww]
                        out = cv2.resize(out, (224, 224), interpolation=cv2.INTER_CUBIC)
                        return out
                out = cv2.resize(img, (224, 224), interpolation=cv2.INTER_CUBIC)
                return out

        augmentors = [
            Resize(),
            imgaug.RandomOrderAug(
                [imgaug.Brightness(30, clip=False),
                 imgaug.Contrast((0.8, 1.2), clip=False),
                 imgaug.Saturation(0.4, rgb=False),
                 imgaug.Lighting(0.1,
                                 eigval=[0.2175, 0.0188, 0.0045][::-1],
                                 eigvec=np.array(
                                     [[-0.5675, 0.7192, 0.4009],
                                      [-0.5808, -0.0045, -0.8140],
                                      [-0.5836, -0.6948, 0.4203]],
                                     dtype='float32')[::-1, ::-1]
                                 )]),
            imgaug.Clip(),
            imgaug.Flip(horiz=True),
            imgaug.ToUint8()
        ]
    else:
        augmentors = [
            imgaug.ResizeShortestEdge(256),
            imgaug.CenterCrop((224, 224)),
            imgaug.ToUint8()
        ]
    ds = AugmentImageComponent(ds, augmentors, copy=False)
    if isTrain:
        ds = PrefetchDataZMQ(ds, min(20, multiprocessing.cpu_count()))
    ds = BatchData(ds, BATCH_SIZE, remainder=not isTrain)
    return ds


def get_config():
    dataset_train = get_data('train')
    dataset_val = get_data('val')

    return TrainConfig(
        model=Model(),
        dataflow=dataset_train,
        callbacks=[
            ModelSaver(),
            InferenceRunner(dataset_val, [
                ClassificationError('wrong-top1', 'val-error-top1'),
                ClassificationError('wrong-top5', 'val-error-top5')]),
            ScheduledHyperParamSetter('learning_rate',
                                      [(30, 1e-2), (60, 1e-3), (85, 1e-4), (95, 1e-5)]),
        ],
        steps_per_epoch=5000,
        max_epoch=110,
    )


def viz_cam(model_file, data_dir):
    ds = get_data('val')
    pred_config = PredictConfig(
        model=Model(),
        session_init=get_model_loader(model_file),
        input_names=['input', 'label'],
        output_names=['wrong-top1', 'bnlast/Relu', 'linearnew/W'],
        return_input=True
    )
    meta = dataset.ILSVRCMeta().get_synset_words_1000()

    pred = SimpleDatasetPredictor(pred_config, ds)
    cnt = 0
    for inp, outp in pred.get_result():
        images, labels = inp
        wrongs, convmaps, W = outp
        batch = wrongs.shape[0]
        for i in range(batch):
            if wrongs[i]:
                continue
            weight = W[:, [labels[i]]].T    # 512x1
            convmap = convmaps[i, :, :, :]  # 512xhxw
            mergedmap = np.matmul(weight, convmap.reshape((512, -1))).reshape(14, 14)
            mergedmap = cv2.resize(mergedmap, (224, 224))
            heatmap = viz.intensity_to_rgb(mergedmap, normalize=True)
            blend = images[i] * 0.5 + heatmap * 0.5
            concat = np.concatenate((images[i], heatmap, blend), axis=1)

            classname = meta[labels[i]].split(',')[0]
            cv2.imwrite('cam{}-{}.jpg'.format(cnt, classname), concat)
            cnt += 1
            if cnt == 500:
                return


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.', required=True)
    parser.add_argument('--data', help='ILSVRC dataset dir')
    parser.add_argument('--depth', type=int, default=18)
    parser.add_argument('--load', help='load model')
    parser.add_argument('--cam', action='store_true')
    args = parser.parse_args()

    DEPTH = args.depth
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

    if args.cam:
        BATCH_SIZE = 128    # something that can run on one gpu
        viz_cam(args.load, args.data)
        sys.exit()

    nr_gpu = get_nr_gpu()
    BATCH_SIZE = TOTAL_BATCH_SIZE // nr_gpu

    logger.auto_set_dir()
    config = get_config()
    if args.load:
        config.session_init = get_model_loader(args.load)
    config.nr_tower = nr_gpu
    SyncMultiGPUTrainer(config).train()
