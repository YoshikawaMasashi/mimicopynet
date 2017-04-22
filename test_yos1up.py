#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sat Dec 24 18:25:35 2016

@author: marshi, yos1up
"""

import numpy as np
import sys, time
from chainer import Chain, ChainList, cuda, gradient_check, Function, Link, optimizers, serializers, utils, Variable
from chainer import functions as F
from chainer import links as L
import mimicopynet
import glob
import gc
from pastalog import Log
log_a = Log('http://localhost:8120', 'mimicopynet/test_yos1up')

"""
class shokunin: # ルールベースアルゴリズムをためしてみる  ←musicnetいじってて絶望的に感じた。
    def __init__(self, a_nn):
        self.nns = np.arange(24, 96)
        self.filters = get_filters(self.nns, siz=5120)
        self.a_nn = a_nn
    def __call__(self, x, mode='train'):
        '''
        x <Variable (bs, ssize*512) int32>
        returns <Variable (bs, 128(=self.a_dim), ssize) float32> 0〜1で出して。
        '''
        x = x.data
        center = x.shape[1]//2
        specs = get_spectrum(x, center, self.filters) # (#filter, #batch)
        specs = specs.T # (#batch, #filter)

        filter_mex = [-0.3, 1.6, -0.3]
"""


def export_graph(filename, vs):
    import chainer.computational_graph as ccg
    g = ccg.build_computational_graph(vs)
    with open(filename, 'w') as o:
        o.write(g.dump())

def lot_statistics(lot, normalize=False):
    assert(normalize==False) # これは禁じ手では・・・
    # （本番で使えない。BatchNormalizationを一層目にやるならわかるが・・・そういうのって普通？）
    if len(lot)>50000 and not normalize:
        print('lot_statistics: skipped.')
        return
    print('========= lot_statistics =========')
    x = np.array([tup[0] for tup in lot])
    if len(x)>0:
        mu_x = np.mean(x, axis=0)
        print('mean of x:')
        print(mu_x)
        sigma_x = np.std(x, axis=0)
        print('std of x:')
        print(sigma_x)
    y = np.array([tup[1] for tup in lot])
    if len(y)>0:
        print('mean of y:')
        print(np.mean(y, axis=0))
        print('std of y:')
        print(np.std(y, axis=0))
    print('==================================')
    if normalize:
        return [((tup[0]-mu_x)/sigma_x, tup[1]) for tup in lot]
    return


def in_npy_out_npy_to_lot(in_npy, out_npy, q_sample=5120, a_sample=10, a_nn=list(range(128)), posratio=None, shuffle=True):
    '''
    list_of_tuple形式に変換します
    in_npy <np.array (S)> : 波形データ。ここでSはwaveのサンプル数
    out_npy <np.array (128, N)> : ピアノロールデータ。ここでNはピアノロールのサンプル数。
    a_nn <list of int> : ピアノロールのうち教師データとするノートナンバーのリスト（1音高だけ耳コピさせたい時は [60] などと指定）
    posratio <None or float> : 教師データが0ベクトルではないようなサンプルが全体のうちこの割合になるように調整する(どうやって？)

    各 i について、
    ピアノロールデータ out_npy　の　[i : i+a_sample] サンプル目　と、
    波形データ in_npy の round(S/N * (i + (a_sample-1)/2) - (q_sample-1)/2)サンプル目から連続するq_sampleサンプルと対応させます。
    TODO: これは妥当か？

    returns: list_of_tuple形式 各qはnp.array (r), 各aはnp.array (128)
    '''
    s = in_npy.shape[0]
    n = out_npy.shape[1]
    qa = []
    padded_in_npy = np.array([in_npy[0]] * q_sample + list(in_npy) + [in_npy[-1]] * q_sample)
    for i in range(n - a_sample):
        start = q_sample + int(np.round(s/n * (i+(a_sample-1)/2) - (q_sample-1)/2))
        qa.append((padded_in_npy[start:start+q_sample], out_npy[a_nn,i:i+a_sample]))

    if (posratio is not None):
        # 正例の割合を調整するために、負例を減らす
        # positive segment の個数を調整しよう

        # test_on_mnnpzをでposratioのオンオフで成績がめっちゃ変わる　なんでだろう
        # →選ばれるサンプルは全然ランダムではないことがわかった。そこで、直後の3行を追加する。

        # あと、そもそも負例を減らした状態でのF値と生の状態でのF値は大きく変わってしまう
        # 負例は減らさずに、別の工夫をした方が良いかも？prioritized learningとか。
        if (shuffle==False):
            print('WARNING: posratio forces shuffling.')
        np.random.shuffle(qa)
        pc = [np.count_nonzero(np.sum(d[1],axis=0)) for d in qa] # positive count
        sorted_idx = np.argsort(pc)[::-1]
        qa_new = []
        current_posratio = np.zeros(2)
        for i in sorted_idx: # positive segmentが多いデータから順に放り込んでいく。
            qa_new.append(qa[i])
            current_posratio += np.array([pc[i], a_sample])
            if (current_posratio[0]/current_posratio[1] < posratio): break
        print('target posratio:', posratio)
        print('achieved posratio:',current_posratio[0]/current_posratio[1])
        qa = qa_new
    if (shuffle):
        # 最後にシャッフル
        np.random.shuffle(qa) # list_of_tuple形式は順不同とする (TODO: この点で、set_of_tupleの方が妥当？)
    return qa


def mndata_to_lot(mndata, q_sample=5120, a_sample=1, a_nn=list(range(128)), posratio=None, stride=1, shuffle=True):
    '''
    list_of_tuple形式に変換します
    mndata : mnnpzファイル(.npz)をnp.loadして得られるオブジェクト
    a_nn <list of int> : ピアノロールのうち教師データとするノートナンバーのリスト（1音高だけ耳コピさせたい時は [60] などと指定）
    posratio <None or float> : 教師データが0ベクトルではないようなサンプルが全体のうちこの割合になるように調整する(どうやって？)
    stride <int> : mndataに入ってるピアノロールのサンプル点から、いくつおきにlotへ抽出するか（デフォルト：1　すなわち全部）
    各 i について、
    ピアノロールデータ score　の　[i : i+a_sample] サンプル目　と、
    波形データ wave の round( score_sample[i + (a_sample-1)/2] - (q_sample-1)/2)サンプル目から連続するq_sampleサンプルと対応させます。   
    基本的には a_sample==1 推奨です。(mndataでは score_sampleが時間順に並んでいる保証はないので)
    returns: list_of_tuple形式 各qはnp.array (r), 各aはnp.array (128)
    '''
    if (a_sample != 1):
        print('Warning: a_sample != 1')
    wave, score, score_sample = mndata['wave'], mndata['score'], mndata['score_sample']
    s = wave.shape[0]
    n = score.shape[1]
    qa = []
    padded_wave = np.array([wave[0]] * q_sample + list(wave) + [wave[-1]] * q_sample)
    for i in range(0, n - a_sample + 1, stride):
        # start = q_sample + int(np.round(s/n * (i+(a_sample-1)/2) - (q_sample-1)/2))
        start = q_sample + int(score_sample[int(np.round(i+(a_sample-1)/2))] - (q_sample-1)/2)
        qa.append((padded_wave[start:start+q_sample], score[a_nn,i:i+a_sample]))
    if (posratio is not None):
        # 正例の割合を調整するために、負例を減らす
        # positive segment の個数を調整しよう
        # test_on_mnnpzをでposratioのオンオフで成績がめっちゃ変わる　なんでだろう
        # →選ばれるサンプルは全然ランダムではないことがわかった。そこで、直後の3行を追加する。
        if (shuffle==False):
            print('WARNING: posratio forces shuffling.')
        np.random.shuffle(qa)
        pc = [np.count_nonzero(np.sum(d[1],axis=0)) for d in qa] # positive count
        sorted_idx = np.argsort(pc)[::-1]
        qa_new = []
        current_posratio = np.zeros(2)
        for i in sorted_idx: # positive segmentが多いデータから順に放り込んでいく。
            qa_new.append(qa[i])
            current_posratio += np.array([pc[i], a_sample])
            if (current_posratio[0]/current_posratio[1] < posratio): break
        print('target posratio:', posratio)
        print('achieved posratio:',current_posratio[0]/current_posratio[1])
        qa = qa_new
    if (shuffle):
        # 最後にシャッフル
        np.random.shuffle(qa) # list_of_tuple形式は順不同とする (TODO: この点で、set_of_tupleの方が妥当？)
    return qa


"""
def test_on_mid(model, mid_file, showroll=True):
    '''
    midiファイルをどれくらい耳コピできるかを調べます
    model <chainer.Chain>: エージェントのモデル。というか学習済みのTransNet3()。update(x,t,mode)が存在している必要あり。
    mid_file <string>: テストしたいmidiファイル　（同名の.wavファイルが（存在しない場合）作られます。）
    a_nn <list of int>: テストするノートナンバーのリスト
    TODO: ノートナンバーごとに正答率見たい？そうでもない？ (現状、集計をTransNet3に任せてるのですぐ対応は難しいかも）
    →むしろTransNet3にノートナンバーごと正答率を求める機能をつければ良いのでは？
    '''
    data_in, data_out = mid_to_in_npy_out_npy(mid_file)
    lot = in_npy_out_npy_to_lot(data_in, data_out,
                                q_sample=model.fmdl.ssize*model.fmdl.slen, # 10 * 512 # 1 * 5120
                                a_sample=model.fmdl.ssize*model.fmdl.a_dim, # 10 * 1 # 1 * 1
                                a_nn=model.a_nn, shuffle=False)
    # ▲ in_npyではmu-law量子化済みになっている。他方、mnnpz形式では生波形のままになっている。注意！
    t0 = time.time()
    print('start testing.....................(',mid_file,')')
    bs_normal = 300
    mode = 'test'
    data = lot
    o_roll = [] # output roll
    a_roll = [] # answer roll
    for idx in range(0,len(data),bs_normal):
        batch = data[idx:idx+bs_normal]
        bs = len(batch)
        x = Variable(np.array([b[0] for b in batch]).astype(np.int32))
        t = Variable(np.array([b[1] for b in batch]).astype(np.float32))
        model.update(x,t, mode=mode)
        o_roll += list(model.lastoutput.transpose(1,0,2).reshape(len(model.a_nn), -1).T) # リストの各要素は　長さ a_dim のリストである
        a_roll += list(model.lastanswer.transpose(1,0,2).reshape(len(model.a_nn), -1).T) # リストの各要素は　長さ a_dim のリストである
        if (time.time() - t0 > 600 or idx+bs_normal>=len(data)):
            t0 = time.time()
            print('mode', mode, '(',idx,'/',len(data),') aveloss', model.aveloss(clear=(idx+bs_normal>=len(data))))
            acc = model.getacctable(clear=(idx+bs_normal>=len(data)))
            precision = acc[1,1]/np.sum(acc[1,:])
            recall = acc[1,1]/np.sum(acc[:,1])
            fvalue = 2*precision*recall/(recall+precision)
            print('acctable:  ', 'P:',precision,'R:',recall,'F:',fvalue)
            print(acc)
    print('done testing.')
    if (showroll):
        import matplotlib.pyplot as plt
        o_roll = np.array(o_roll).T
        a_roll = np.array(a_roll).T
        idx = np.linspace(0, o_roll.shape[1], 100).astype(np.int32)[:-1]
        plt.subplot(211)
        plt.imshow((o_roll[:,idx] > 0.5).astype(np.int32))
        plt.subplot(212)
        plt.imshow(a_roll[:,idx])
        plt.show()
"""

def test_on_mnnpz(model, mnnpz_file, showroll=True):
    '''
    mnnpzファイルをどれくらい耳コピできるかを調べます
    model <chainer.Chain>: エージェントのモデル。というか学習済みのTransNet3()。update(x,t,mode)が存在している必要あり。
    mnnpz_file <string>: テストしたいmnnpzファイル
    a_nn <list of int>: テストするノートナンバーのリスト
    TODO: ノートナンバーごとに正答率見たい？そうでもない？ (現状、集計をTransNet3に任せてるのですぐ対応は難しいかも）
    →むしろTransNet3にノートナンバーごと正答率を求める機能をつければ良いのでは？
    '''
    lot = mnnpz_to_lot(mnnpz_file, q_sample=model.fmdl.ssize*model.fmdl.slen, # 10 * 512 # 1 * 5120
                                a_sample=model.fmdl.ssize, # 10 # 1
                                a_nn=model.a_nn, shuffle=False, posratio=None)
    print('start testing.....................(',mnnpz_file,')')
    train_and_test(model, [], lot, epochnum=0, bs_normal=100, mcmcstepnum=0, showroll=showroll)

def mid_to_in_npy_out_npy(mid_file):
    '''
    midファイルから、in_npyとout_npyの組を返します。

    具体的には
    .midから.wavを生む
    .midと.wavから.mid.npyと.wav.npyを生む
    .mid.npyと.wav.npyからin_npyとout_npyを読み込み、返す
    ということをやります

    すでに生成済みのファイルは再生成しません。
    '''
    import os.path
    base, ext = os.path.splitext(mid_file)
    assert(ext==".mid")
    wav_file = base+".wav"
    midnpy_file = base+".mid.npy"
    wavnpy_file = base+".wav.npy"

    if not(os.path.exists(wavnpy_file)):
        if not(os.path.exists(wav_file)):
            mimicopynet.data.midi_to_wav(mid_file, wav_file)
        mimicopynet.data.wav_to_input(wav_file, wavnpy_file)

    if not(os.path.exists(midnpy_file)):
        mimicopynet.data.midi_to_output(mid_file, midnpy_file)

    return np.load(wavnpy_file), np.load(midnpy_file)

def mid_to_mnnpz(mid_file, mnnpz_file):
    '''
    midファイルから、mnnpzファイルへ変換します。

    具体的には
    .midから.wavを生む
    .midから.mid.npyを生む
    .mid.npyと.wavからmnnpzを生成。
    mnnpzを読み込み、返す
    ということをやります

    すでに生成済みのファイルは再生成しません。
    '''
    wav_fre = 44100
    train_sample = 512.0
    import os.path
    base, ext = os.path.splitext(mid_file)
    assert(ext==".mid")
    wav_file = base+".wav"
    midnpy_file = base+".mid.npy"
    mnnpz_file = base+"_mnnpz.npz"
    if not os.path.exists(mnnpz_file):
        if not(os.path.exists(wav_file)):
            mimicopynet.data.midi_to_wav(mid_file, wav_file)
        if not(os.path.exists(midnpy_file)):
            mimicopynet.data.midi_to_output(mid_file, midnpy_file, wav_fre=wav_fre, train_sample=train_sample)
        # midi_to_wavが44100Hzで出力することが仮定されている。大丈夫か？
        from scipy.io import wavfile
        wav = np.mean(sp.io.wavfile.read(wav_file)[1],axis=1).astype(np.float64) / 2**15 # モノラル化して-1〜1に正規化
        midnpy = np.load(midnpy_file)
        np.savez(mnnpz_file, wave=wav, score=midnpy, score_sample=np.arange(midnpy.shape[1])*wav_fre/train_sample)
    return np.load(mnnpz_file)
    

def mid_to_lot(mid_file, q_sample=5120, a_sample=1, a_nn=list(range(128)), posratio=None, samplenum=None, shuffle=True):
    '''
    mid_to_in_npy_out_npy → in_npy_out_npy_to_lot
    mid_file <str>: midファイル名　ただしglob.globに渡せるワイルドカード表現も可能。該当するファイル全てからデータを取得する。
    samplenum <None or int>: サンプル数の上限。
    '''
    files = sorted(glob.glob(mid_file))
    print("files matched:", len(files))
    lot = []
    for i,file in enumerate(files):
        print(i,"/",len(files),":",file,'(num of samples collected:',len(lot),')')
        in_npy, out_npy = mid_to_in_npy_out_npy(file)
        lot += in_npy_out_npy_to_lot(in_npy, out_npy, q_sample=q_sample, a_sample=a_sample, a_nn=a_nn, posratio=posratio, shuffle=shuffle)
        if (samplenum is not None and samplenum <= len(lot)):
            lot = lot[:samplenum]
            break
    if (shuffle):
        # 最後にシャッフル
        np.random.shuffle(lot)
    return lot


def mnnpz_to_lot(mnnpz_file, q_sample=5120, a_sample=1, a_nn=list(range(128)), posratio=None, stride=1, samplenum=None, shuffle=True):
    '''
    mnnpz_file <str>: mnnpzファイル名　ただしglob.globに渡せるワイルドカード表現も可能。該当するファイル全てからデータを取得する。
    samplenum <None or int>: lotに含まれるサンプル数の上限。
    '''
    files = sorted(glob.glob(mnnpz_file))
    print("files matched:", len(files))
    lot = []
    for i,file in enumerate(files):
        print(i,"/",len(files),":",file,'(num of samples collected:',len(lot),')')
        data = np.load(file)
        lot += mndata_to_lot(data, q_sample=q_sample, a_sample=a_sample, a_nn=a_nn, posratio=posratio, stride=stride, shuffle=shuffle)
        if (samplenum is not None and samplenum <= len(lot)):
            lot = lot[:samplenum]
            break
    if (shuffle):
        # 最後にシャッフル
        np.random.shuffle(lot)
    return lot    


def get_train_test_lot(trainpath, trainsample, testpath, testsample, stride=2, shuffle=True, posratio=None):
    '''
    trainpath = '/Users/yoshidayuuki/Downloads/musicnet/musicnet_data/2*/data.npz'
    trainsample = 375000
    testpath = '/Users/yoshidayuuki/Downloads/musicnet/musicnet_data/1*/data.npz'
    testsample = 125000
    '''
    train_lot = mnnpz_to_lot(trainpath,q_sample=slen*ssize, a_sample=ssize, a_nn=a_nn, posratio=posratio, stride=stride, samplenum=trainsample, shuffle=shuffle)
    test_lot = mnnpz_to_lot(testpath,q_sample=slen*ssize, a_sample=ssize, a_nn=a_nn, posratio=posratio, stride=stride, samplenum=testsample, shuffle=shuffle)
    print('train data len', len(train_lot))
    print('test data len', len(test_lot))
    print('q data shape:',train_lot[0][0].shape)
    print('a data shape:',train_lot[0][1].shape)
    return train_lot, test_lot


def train_and_test(mdl, train_lot, test_lot, epochnum=100, bs_normal=100, mcmcstepnum=0, showroll=False):
    gc.collect() # メモリ逃がそう
    # データ前処理
    train_lot = mdl.fmdl.preprocess(train_lot)
    lot_statistics(train_lot)
    test_lot = mdl.fmdl.preprocess(test_lot)
    lot_statistics(test_lot)
    gc.collect() # メモリ逃がそう
    if mcmcstepnum>0: # 推論を大きく誤った訓練データを優先的に学習させるオプション
        # posterior = np.ones(len(data_train))/ansnum # 各訓練データについて「最近の回答時に正解ラベルに対して出力した事後確率」を格納しておくarray
        logposterior = -np.inf + np.zeros(len(train_lot))
        accepted_frac = np.zeros(2)
    fmax = np.zeros(2)
    t0 = time.time()

    for epoch in range(epochnum+1):
        print('epoch', epoch)
        for data,mode in [(train_lot,'train'), (test_lot,'test')]:
            if epoch==0 and mode=='train': continue
            if len(data)==0: continue
            o_roll = [] # output roll
            a_roll = [] # answer roll            
            if mode=='test' or mcmcstepnum==0: # 優先学習なし
                shuffled_idx = np.arange(len(data))
                np.random.shuffle(shuffled_idx)
                for idx in range(0,len(data),bs_normal):
                    batch = np.array(data)[shuffled_idx[idx:idx+bs_normal]]
                    bs = len(batch)
                    x = Variable(xp.array([b[0] for b in batch]))
                    t = Variable(xp.array([b[1] for b in batch]).astype(xp.float32))
                    mdl.update(x,t, mode=mode)
                    if showroll:
                        o_roll += list(mdl.lastoutput.transpose(1,0,2).reshape(len(mdl.a_nn), -1).T) # リストの各要素は　長さ a_dim のリストである
                        a_roll += list(mdl.lastanswer.transpose(1,0,2).reshape(len(mdl.a_nn), -1).T) # リストの各要素は　長さ a_dim のリストである                    
            else: #優先学習あり
                bnum = max(len(data)//bs_normal, 1)
                # 「大きく誤った訓練データが高確率で含まれるようにバッチを構成する」
                # ということを bnum 回繰り返す。
                for i in range(bnum):
                    idx = np.random.choice(len(data), bs_normal) # まず、叩き台のバッチを構成する。これは完全にランダム。
                    
                    for j in range(mcmcstepnum): # 上記で構成したバッチに修正を施す。「優先学習」が反映されるように。
                        # 確率の比が 1/(最近の回答時に正解ラベルに対して出力した事後確率) となるように
                        # 訓練データを選出したい。
                        # 真面目にやるのではなく、MCMC（メトロポリス法）を使ってサンプリングすることを考え、
                        # その最初の数ステップだけをやる、という実装。
                        idx_sugg = np.random.choice(len(data), bs_normal) # 提案
                        # acc_prob = np.minimum(1., posterior[idx]/(posterior[idx_sugg]+1e-12)) # 受理される確率
                        acc_prob = np.exp(np.minimum(0., logposterior[idx] - logposterior[idx_sugg]))
                        accidx = np.where(acc_prob > np.random.rand(bs_normal))[0] # 受理されたインデクス
                        accepted_frac += np.array([len(accidx), bs_normal])
                        idx[accidx] = idx_sugg[accidx] # バッチの更新
                    batch = np.array(data)[idx]
                    bs = len(batch)
                    x = Variable(xp.array([b[0] for b in batch]))
                    t = Variable(xp.array([b[1] for b in batch]).astype(xp.float32))
                    mdl.update(x,t, mode=mode)
                    logposterior[idx] = mdl.logposterior
            # 1epoch 終了後の処理
            endflg = True # epoch途中で表示していた頃の名残
            note = ''
            if mode=='train' and mcmcstepnum>0:
                note = '[with priority (mcmc:'+str(mcmcstepnum)+')(accepted:'+str(accepted_frac[0]/accepted_frac[1])+')]'
                accepted_frac = np.zeros(2)
            print('mode', mode, note,'(len(data) =',len(data),') aveloss', mdl.aveloss(clear=endflg))
            acc = mdl.getacctable(clear=endflg)
            precision = acc[1,1]/np.sum(acc[1,:])
            recall = acc[1,1]/np.sum(acc[:,1])
            fvalue = 2*precision*recall/(recall+precision)
            fmax[mode=='test'] = max(fmax[mode=='test'], fvalue)
            print('acctable:  ', 'P:',precision,'R:',recall,'F:',fvalue,'(Fmax:',fmax[mode=='test'],')')
            log_a.post(mode+'F', value=fmax[mode=='test'], step=epoch)
            print(acc)
            if mode=='test' and showroll:
                import matplotlib.pyplot as plt
                o_roll = np.array(o_roll).T
                a_roll = np.array(a_roll).T
                idx = np.linspace(0, o_roll.shape[1], 100).astype(np.int32)[:-1]
                plt.subplot(211)
                plt.imshow((o_roll[:,idx] > 0.5).astype(np.int32))
                plt.subplot(212)
                plt.imshow(a_roll[:,idx])
                plt.show()
            if mode=='test' and fmax[1] == fvalue and epoch>0:
                serializers.save_npz(mdlfilename, mdl.fmdl)
                print('saved: '+mdlfilename)


a_nn = list(np.arange(60, 84)) # [60] 1音に限定した耳コピはもうやめた
a_dim = len(a_nn)
posratio = None # 正例の割合 0.5ももうやめ
slen = 5120
ssize = 1

np.random.seed(0)

gpu = None
fmdl = mimicopynet.model.yosnet_ft(a_nn=a_nn)
# fmdl = mimicopynet.model.yosnet(a_dim=a_dim, slen=slen, ssize=ssize)
# fmdl = mimicopynet.model.wavenet(a_dim=a_dim, embed_dim=1)
if gpu is not None:
    cuda.get_device(gpu[0]).use()
    fmdl.to_gpu(gpu[0])
    xp = cuda.cupy
else:
    xp = np
mdl = mimicopynet.model.TransNet3(fmdl=fmdl, a_nn=a_nn)
# TransNet3: エージェント(learner)
# yosnet, wavenet: 脳


mdlfilename = "fmdl6084.model"
musicnet_data_dir = '/Users/yoshidayuuki/Downloads/musicnet/musicnet_data/'
try:
    gototraining
    serializers.load_npz(mdlfilename, mdl.fmdl)
    print('loaded.')
    # gototraining
except: 
    print('welcome to training!')
    train_lot, test_lot = get_train_test_lot(musicnet_data_dir + '2*/data.npz', 375000, musicnet_data_dir + '1*/data.npz', 125000, stride=16, posratio=posratio)
    train_and_test(mdl, train_lot, test_lot, epochnum=100, bs_normal=100, mcmcstepnum=0)

for f in glob.glob(musicnet_data_dir + "1*/data.npz"):
    test_on_mnnpz(mdl, f, showroll=False)


'''
def harmonic_score(n):
    """
    ノートナンバー n との調和度の高さ（同時に鳴った時にnが鳴ってるかの判定が難しくなる度合い）を
    各ノートナンバーについて求めて、長さ128のarrayで返します。
    (make_random_songの引数lmb_startとlmb_stopに与える用)
    """
    harmonic_dnn = [12, 19, 24, 28, 31, 34, 36, 38, 40]
    harmonic_dnn = [-i for i in harmonic_dnn][::-1] + [0] + harmonic_dnn
    ret = np.ones(128).astype(np.float32) # 1 が標準としよう。
    for i in harmonic_dnn:
        if (0 <= n+i < 128): ret[n+i] = 50.0 # 5はすごい
    return ret
lmb_start = list(50. / harmonic_score(60))
lmb_stop = 1.
mimicopynet.data.make_random_song("hard.mid", lmb_start=lmb_start, lmb_stop=lmb_stop)
mimicopynet.data.midi_to_wav("hard.mid","hard.wav")
mimicopynet.data.midi_to_output("hard.mid","hard.mid.npy")
mimicopynet.data.wav_to_input("hard.wav","hard.wav.npy")
'''

'''
mimicopynet.data.make_random_song("train.mid")
mimicopynet.data.midi_to_wav("train.mid","train.wav")
mimicopynet.data.midi_to_output("train.mid","train_out.npy")
mimicopynet.data.wav_to_input("train.wav","train_in.npy")
'''

'''
mdl = mimicopynet.model.TransNet()
mdl.set_training_data(train_in, train_out)
while 1:
    for i in range(10):
        mdl.learn(size=10)
    print('aveloss', mdl.aveloss(clear=True))
    print('acctable', mdl.getacctable(clear=True))
'''
'''
rescale_input = False

# 0〜255を-1〜1にリスケール
if (rescale_input):
    train_in = train_in / 128 - 1.
#0 or 1
# train_out
'''


'''
モデルについて思うこと
・batch normalizationした方が良い?（batchsize100で1より収束CPU時間が遅くなっている？）
 ・音のうるささに関してだけEmbedIDは遅いだろうし意味はなさそう？（「入力の」1次元を16次元に展開する意味は？）
 ・softmax_cross_entropy的なlossのほうが良いだろう
 ・正例の個数を調節
 ・一音耳コピを
 ・バッチサイズあげて遅くなってるのはメモリのせいかも
　bs=100で10GBくらい食ってるけどそういうもんだっけ→取り急ぎ30に減らした
・ネットワークの要見直し
・高速化？ 20層は重たい・・・


　・random songはマシンにとってはとても簡単であることが判明したので実際のソングにしよう

・過学習がひどい（train F: 0.94、valid F: 0.56、test F: 0.28）。q_sampleを512から増やした方が良いか。
slenとssizeをが512と10だったが5120と1にする
yosnetの中身は、512-512-256-1 だったのを 5120-1280-320-1にする
batchsizeは30だったのを300にする（高速化のため。5倍くらい早くなった）

相変わらず過学習がひどい（train F: 0.99(40epoch), valid F: 0.77(2epoch)）
次元じゃなくてサンプル数増やさないとダメかも。今は約16万サンプル
これを50万サンプルまで増やそう




・オンラインNMF？



midiworldで、正常にwav変換できなかったファイルたち
A_Teens/Super_Trouper.mid

midiworldで、正常にmid.npy変換できなかったファイルたち
Aaron_Neville/Tell_It_Like_It_Is.mid
'''







