#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sat Dec 24 18:25:35 2016

@author: marshi
"""

import numpy as np
import chainer
from chainer import cuda, Function, gradient_check, report, training, utils, Variable
from chainer import datasets, iterators, optimizers, serializers
from chainer import Link, Chain, ChainList
import chainer.functions as F
import chainer.links as L
from chainer.training import extensions
from ..chainer_util import f_measure_accuracy
from ..data import make_cqt_input, score_to_midi

class BasicCNN_(chainer.Chain):
    '''
    下のBasicCNNで使うChain
    '''
    def __init__(self, input_cnl=1):
        '''
        input_cnl: 画像のチャンネル（CQTの絶対値を使うなら1,実部と虚部を使うなら2)
        '''
        #TODO: input_cnl mode などはconfigクラスとして一まとめにした方が良いかも
        super(BasicCNN_, self).__init__(
            conv1 = L.Convolution2D(input_cnl, 4, ksize=(13,13), pad=(6,6)),
            conv2 = L.Convolution2D(4, 8, ksize=(13,13), pad=(6,6)),
            conv3 = L.Convolution2D(8, 16, ksize=(13,13), pad=(6,6)),
            conv4 = L.Convolution2D(16, 128, ksize=(84,1), pad=(0,0)),
            bn1 = L.BatchNormalization(input_cnl),
            bn2 = L.BatchNormalization(4),
            bn3 = L.BatchNormalization(8),
            bn4 = L.BatchNormalization(16)
        )

    def __call__(self, x, test=False):
        '''
        x: Variable (bs, 1, pitchs, width)

        ret: Variable (bs, pitchs, width)
            sigmoidをかけない値を出力する．
        '''
        h = x

        h = self.bn1(h, test=test)
        h = self.conv1(h)
        h = F.leaky_relu(h)

        h = self.bn2(h, test=test)
        h = self.conv2(h)
        h = F.leaky_relu(h)

        h = self.bn3(h, test=test)
        h = self.conv3(h)
        h = F.leaky_relu(h)

        h = self.bn4(h, test=test)
        h = self.conv4(h)

        h = h[:,:,0]
        return h

class BasicCNN(object):
    '''
    スペクトル×時間の２次元画像から，それぞれの時間における耳コピを行うモデル
    self.model: BasicCNN_のインスタンス
    self.classifier: 分類するためのクラス
    '''
    def __init__(self, input_cnl=1):
        '''
        input_cnl: 画像のチャンネル（CQTの絶対値を使うなら1,実部と虚部を使うなら2)
        '''
        self.model = BasicCNN_(input_cnl=input_cnl)
        self.classifier = L.Classifier(self.model, F.sigmoid_cross_entropy,
                                       f_measure_accuracy)

        self.optimizer = optimizers.Adam()
        self.optimizer.setup(self.classifier)
    def load_cqt_inout(self, file):
        '''
        spect np.narray [chl, pitch, seqlen]
        score np.narray [pitch, seqlen]
        '''
        data = np.load(file)
        score = data["score"]
        spect = data["spect"]

        width = 128
        length = spect.shape[2]

        spect = [spect[:,:,i*width:(i+1)*width]
                 for i in range(int(length/width))]
        self.spect = np.array(spect).astype(np.float32)
        score = [score[:,i*width:(i+1)*width] for i in range(int(length/width))]
        self.score = np.array(score).astype(np.int32)
        print("loaded!",self.spect.shape, self.score.shape)
    def eval_call(self, x, t):
        '''
        テスト用にClassifierを呼ぶ
        self.classifier(x, t)と同様の使い方をする．
        '''
        self.classifier(x, True, t)
    def learn(self):
        '''
        学習をするメソッド
        '''
        dataset = chainer.datasets.TupleDataset(self.spect, self.score)
        p = 0.999
        trainn = int(p*len(dataset))
        print(trainn,len(dataset)-trainn)
        train,test = chainer.datasets.split_dataset_random(dataset, trainn)

        train_iter = iterators.SerialIterator(train, batch_size=1, shuffle=True)
        test_iter = iterators.SerialIterator(test, batch_size=2, repeat=False,
                                             shuffle=False)

        updater = training.StandardUpdater(train_iter, self.optimizer)
        trainer = training.Trainer(updater, (300000, 'iteration'), out='result')

        trainer.extend(extensions.Evaluator(test_iter, self.classifier,
                                            eval_func=self.eval_call),
                                            trigger=(500, 'iteration'))
        trainer.extend(extensions.LogReport(trigger=(50, 'iteration')))
        trainer.extend(extensions.PrintReport(['iteration', 'main/accuracy',
                                               'main/loss',
                                               'validation/main/accuracy',
                                               'validation/main/loss']))
        trainer.extend(extensions.ProgressBar(update_interval=5))
        trainer.extend(extensions.snapshot_object(self.model,
                                            'model_{.updater.iteration}.npz',
                                            serializers.save_npz,
                                            trigger=(500, 'iteration')))
        trainer.run()
    def load_model(self, file):
        serializers.load_npz(file, self.model)
    def __call__(self, input_data):
        '''
        学習したモデルを動かします

        input: np.narray [chl, pitch, seqlen]
        ret:  np.narray [pitch, seqlen]
        '''
        width = 128
        length = input_data.shape[2]
        input_data = [input_data[:,:,i*width:(i+1)*width]
                      for i in range(int(length/width)+1)]
        input_data = [np.expand_dims(input_data_, axis=0)
                      for input_data_ in input_data]

        score = []
        for input_data_ in input_data:
            score_ = (self.model(input_data_, test=True)[0].data>0.)*1
            score.append(score_)

        output = np.concatenate(score, axis=1)
        return output
    def transcript(self, wavfile, midfile, mode='abs'):
        '''
        学習したモデルを使って，耳コピをするメソッド
        wavfile: 耳コピしたいWavファイルのファイル名(44100,2ch想定)
        midfile: 耳コピして生成される，midファイル名
        model: CQTからどんな値を抽出するか
            'abs' 絶対値(chl=1)
            'raw' 実部と虚部をそのままだす(chl=2)
        '''
        input_data = make_cqt_input(wavfile, mode=mode)
        score = self(input_data)
        score_to_midi(score, midfile)
