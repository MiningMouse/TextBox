# @Time   : 2020/11/19
# @Author : Jinhao Jiang
# @Email  : jiangjinhao@std.uestc.edu.cn

from random import sample
import torch
import torch.nn as nn
import torch.nn.functional as F
import math
from textbox.model.abstract_generator import UnconditionalGenerator
from torch.utils.data import DataLoader

class LeakGANGenerator(UnconditionalGenerator):
    def __init__(self, config, dataset):
        super(LeakGANGenerator, self).__init__(config, dataset)

        self.hidden_size = config['hidden_size']
        self.embedding_size = config['generator_embedding_size']
        self.max_length = config['max_seq_length'] + 2  # max_length is the length of (eos + origin_max_len + sos)
        self.monte_carlo_num = config['Monte_Carlo_num']
        self.filter_nums = config['filter_nums']
        self.goal_out_size = sum(self.filter_nums)
        self.goal_size = config['goal_size']
        self.step_size = config['step_size']
        self.temperature = config['temperature']
        self.dis_sample_num = config['d_sample_num']
        self.start_idx = dataset.sos_token_idx
        self.end_idx = dataset.eos_token_idx
        self.pad_idx = dataset.padding_token_idx
        self.vocab_size = dataset.vocab_size
        self.batch_size = dataset.batch_size
        self.use_gpu = config['use_gpu']
        self.gpu_id = config['gpu_id']
        self.eval_generate_num = config['eval_generate_num']

        # self.LSTM = nn.LSTM(self.embedding_size, self.hidden_size)
        self.word_embedding = nn.Embedding(self.vocab_size, self.embedding_size, padding_idx = self.pad_idx)
        self.vocab_projection = nn.Linear(self.hidden_size, self.vocab_size)

        self.worker = nn.LSTM(self.embedding_size, self.hidden_size)
        self.manager = nn.LSTM(self.goal_out_size, self.hidden_size)

        self.work2goal = nn.Linear(self.hidden_size, self.vocab_size * self.goal_size)
        self.mana2goal = nn.Linear(self.hidden_size, self.goal_out_size)
        self.goal2goal = nn.Linear(self.goal_out_size, self.goal_size, bias=False)

        self.goal_init = nn.Parameter(torch.rand((self.batch_size, self.goal_out_size)))

    def pretrain_loss(self, corpus, dis):
        r"""Returns the pretrain_generator Loss for predicting target sequence.

        Args:
            corpus: include input_idx(bs*seq_len), target_idx(bs*seq_len) ,
            dis: discriminator model

        Returns:
            object:

        """
        datas = corpus['target_idx'] # b * max_len
        targets = datas[:, 1:] # b*max_length-1
        batch_size, seq_len = targets.size()
        _, feature_array, goal_array, leak_out_array = self.leakgan_forward(targets, dis, if_sample=False,
                                                                            no_log=False, start_letter=self.start_idx)
        # Manager loss
        mana_cos_loss = self.manager_cos_loss(batch_size, feature_array,
                                              goal_array)  # batch_size * (seq_len / step_size)
        manager_loss = -torch.sum(mana_cos_loss) / (batch_size * (seq_len // self.step_size))

        # Worker loss
        work_nll_loss = self.worker_nll_loss(targets, leak_out_array)  # batch_size * seq_len
        work_nll_loss = torch.sum(work_nll_loss, dim=1) # bs
        targets_length = corpus['target_length'] - 1
        work_nll_loss = work_nll_loss / targets_length
        worker_loss = torch.mean(work_nll_loss)

        return manager_loss, worker_loss

    def calculate_loss(self, corpus, dis):
        r"""Returns the nll for test predicting target sequence.

        Args:
            corpus: include input_idx(bs*seq_len), target_idx(bs*seq_len) ,
            dis: discriminator model

        Returns:
            object:

        """
        datas = corpus['target_idx'] # b * max_len
        targets = datas[:, 1:] # b*max_length-1
        batch_size, seq_len = targets.size()
        _, feature_array, goal_array, leak_out_array = self.leakgan_forward(targets, dis, if_sample=False,
                                                                            no_log=False, start_letter=self.start_idx)

        # Worker loss
        work_nll_loss = self.worker_nll_loss(targets, leak_out_array)  # batch_size * seq_len
        work_nll_loss = torch.sum(work_nll_loss, dim=1)  # bs
        work_nll_loss = work_nll_loss
        worker_loss = torch.mean(work_nll_loss)

        return worker_loss

    def forward(self, idx, inp, work_hidden, mana_hidden, feature, real_goal, no_log=False, train=False):
        r"""Embeds input and sample on token at a time (seq_len = 1)
        Args:
            idx: index of current token in sentence
            inp: current input token for a batch [batch_size]
            work_hidden: 1 * batch_size * hidden_dim
            mana_hidden: 1 * batch_size * hidden_dim
            feature: 1 * batch_size * total_num_filters, feature of current sentence
            real_goal: batch_size * goal_out_size, real_goal in LeakGAN source code
            no_log: no log operation
            train: if train

        Returns:
            out: current output prob over vocab with log_softmax or softmax bs*vocab_size
            cur_goal: bs * 1 * goal_out_size
            work_hidden: 1 * batch_size * hidden_dim
            mana_hidden: 1 * batch_size * hidden_dim
        """
        emb = self.word_embedding(inp).unsqueeze(0)  # 1 * batch_size * embed_dim

        # Manager
        mana_out, mana_hidden = self.manager(feature, mana_hidden)  # mana_out: 1 * batch_size * hidden_dim
        mana_out = self.mana2goal(mana_out.permute([1, 0, 2]))  # batch_size * 1 * goal_out_size
        cur_goal = F.normalize(mana_out, p=2, dim=-1)
        _real_goal = self.goal2goal(real_goal)  # batch_size * goal_size
        _real_goal = F.normalize(_real_goal, p=2, dim=-1).unsqueeze(-1)  # batch_size * goal_size * 1

        # Worker
        work_out, work_hidden = self.worker(emb, work_hidden)  # work_out: 1 * batch_size * hidden_dim
        work_out = self.work2goal(work_out.squeeze(dim=0))  # bs * (vocab*goal)
        work_out = work_out.contiguous().view(-1, self.vocab_size, self.goal_size)  # batch_size * vocab_size * goal_size

        # Sample token
        out = torch.matmul(work_out, _real_goal).squeeze(-1)  # batch_size * vocab_size

        # Temperature control
        if idx > 1:
            if train:
                temperature = 1.0
            else:
                temperature = self.temperature
        else:
            temperature = self.temperature

        out = temperature * out # bs * vocab

        if no_log:
            out = F.softmax(out, dim=-1)
        else:
            out = F.log_softmax(out, dim=-1)

        return out, cur_goal, work_hidden, mana_hidden

    def leakgan_forward(self, targets, dis, if_sample, start_letter=2, no_log=False, train=False):
        r""" Get all feature and goals according to given sentences

        Args:
            targets: batch_size * max_seq_len, include end token and pad token but not include start token
            dis: discriminator model
            if_sample: if use to sample token
            no_log: if use log operation
            train: if use temperature parameter

        Returns: samples, feature_array, goal_array, leak_out_array:
            samples: batch_size * seq_len
            feature_array: batch_size * (seq_len + 1) * total_num_filter
            goal_array: batch_size * (seq_len + 1) * goal_out_size
            leak_out_array: batch_size * seq_len * vocab_size

        """
        batch_size, seq_len = targets.size() # seq_len = max_length - 1

        feature_array = torch.zeros((batch_size, seq_len + 1, self.goal_out_size))
        goal_array = torch.zeros((batch_size, seq_len + 1, self.goal_out_size))
        leak_out_array = torch.zeros((batch_size, seq_len + 1, self.vocab_size))

        samples = torch.zeros(batch_size, seq_len + 1).long()
        samples[:,:] = self.pad_idx
        samples = samples.long()
        work_hidden = self.init_hidden(batch_size)
        mana_hidden = self.init_hidden(batch_size)
        leak_inp = torch.LongTensor([start_letter] * batch_size)
        # dis_inp = torch.LongTensor([start_letter] * batch_size)
        real_goal = self.goal_init[:batch_size, :]

        if self.use_gpu:
            feature_array = feature_array.cuda(self.gpu_id)
            goal_array = goal_array.cuda(self.gpu_id)
            leak_out_array = leak_out_array.cuda(self.gpu_id)
            samples = samples.cuda(self.gpu_id)

        goal_array[:, 0, :] = real_goal  # g0 = goal_init
        for i in range(seq_len + 1):
            # Get feature
            if if_sample:
                dis_inp = samples[:, :seq_len]
            else:  # to get feature and goal
                dis_inp = torch.zeros(batch_size, seq_len).long()
                dis_inp[:, :] = self.pad_idx
                dis_inp = dis_inp.long()
                if i > 0:
                    dis_inp[:, :i] = targets[:, :i]  # cut sentences
                    leak_inp = targets[:, i - 1]

            if self.use_gpu:
                dis_inp = dis_inp.cuda(self.gpu_id)
                leak_inp = leak_inp.cuda(self.gpu_id)
            feature = dis.get_feature(dis_inp).unsqueeze(0)  # !!!note: 1 * batch_size * total_num_filters

            feature_array[:, i, :] = feature.squeeze(0)

            # Get output of one token
            # cur_goal: batch_size * 1 * goal_out_size
            out, cur_goal, work_hidden, mana_hidden = self.forward(i, leak_inp, work_hidden, mana_hidden, feature,
                                                                   real_goal, no_log=no_log, train=train)
            leak_out_array[:, i, :] = out

            # ===My implement according to paper===
            # Update real_goal and save goal
            # if 0 < i < 4:  # not update when i=0
            #     real_goal = torch.sum(goal_array, dim=1)  # num_samples * goal_out_size
            # elif i >= 4:
            #     real_goal = torch.sum(goal_array[:, i - 4:i, :], dim=1)
            # if i > 0:
            #     goal_array[:, i, :] = cur_goal.squeeze(1)  # !!!note: save goal after update last_goal
            # ===LeakGAN origin===
            # Save goal and update real_goal
            goal_array[:, i, :] = cur_goal.squeeze(1)
            if i > 0 and i % self.step_size == 0:
                real_goal = torch.sum(goal_array[:, i - 3:i + 1, :], dim=1)
                if i / self.step_size == 1:
                    real_goal += self.goal_init[:batch_size, :]

            # Sample one token
            if not no_log:
                out = torch.exp(out)
            out = torch.multinomial(out, 1).view(-1)  # [batch_size] (sampling from each row)
            samples[:, i] = out.data
            leak_inp = out

        # cut to seq_len
        samples = samples[:, :seq_len]
        leak_out_array = leak_out_array[:, :seq_len, :]

        return samples, feature_array, goal_array, leak_out_array

    def sample_batch(self):
        self.eval()
        sentences = []
        with torch.no_grad():
            h_prev = torch.zeros(1, self.batch_size, self.hidden_size, device = self.device) # 1 * b * h
            o_prev = torch.zeros(1, self.batch_size, self.hidden_size, device = self.device) # 1 * b * h
            prev_state = (h_prev, o_prev)
            X = self.word_embedding(torch.tensor([self.start_idx] * self.batch_size, dtype = torch.long, device = self.device)).unsqueeze(0) # 1 * b * e
            sentences = torch.zeros((self.max_length, self.batch_size), dtype = torch.long, device = self.device)
            sentences[0] = self.start_idx

            for i in range(1, self.max_length):
                output, prev_state = self.LSTM(X, prev_state)
                P = F.softmax(self.vocab_projection(output), dim = -1).squeeze(0) # b * v
                for j in range(self.batch_size):
                    sentences[i][j] = torch.multinomial(P[j], 1)[0]
                X = self.word_embedding(sentences[i]).unsqueeze(0) # 1 * b * e

            sentences = sentences.permute(1, 0) # b * l

            for i in range(self.batch_size):
                end_pos = (sentences[i] == self.end_idx).nonzero()
                if (end_pos.shape[0]):
                    sentences[i][end_pos[0][0] + 1 : ] = self.pad_idx

        self.train()

        return sentences

    def sample(self, sample_num, dis, start_letter, train=False):
        num_batch = sample_num // self.batch_size + 1 if sample_num != self.batch_size else 1
        samples = torch.zeros(num_batch * self.batch_size, self.max_length-1).long()  # larger than num_samples
        fake_sentences = torch.zeros((self.batch_size, self.max_length-1))
        fake_sentences[:, :] = self.pad_idx

        for b in range(num_batch):
            leak_sample, _, _, _ = self.leakgan_forward(fake_sentences, dis, if_sample=True, no_log=False
                                                        , start_letter=start_letter, train=train)

            assert leak_sample.shape == (self.batch_size, self.max_length-1)
            samples[b * self.batch_size:(b + 1) * self.batch_size, :] = leak_sample

        samples = samples[:sample_num, :]
        if self.use_gpu:
            samples = samples.cuda(self.gpu_id)

        return samples

    def generate(self, eval_data, dis):
        number_to_gen = self.eval_generate_num
        num_batch = number_to_gen // self.batch_size + 1 if number_to_gen != self.batch_size else 1
        samples = torch.zeros(num_batch * self.batch_size, self.max_length-1).long()  # larger than num_samples
        fake_sentences = torch.zeros((self.batch_size, self.max_length-1))
        idx2token = eval_data.idx2token
        
        for b in range(num_batch):
            leak_sample, _, _, _ = self.leakgan_forward(fake_sentences, dis, if_sample=True, no_log=False
                                                        , start_letter=self.start_idx, train=False)

            assert leak_sample.shape == (self.batch_size, self.max_length-1)
            samples[b * self.batch_size:(b + 1) * self.batch_size, :] = leak_sample

        samples = samples[:number_to_gen, :]
        samples = samples.tolist()
        samples = [ [idx2token[w] for w in sen] for sen in samples]

        return samples
    
    def adversarial_loss(self, dis):
        with torch.no_grad():
            gen_samples = self.sample(self.batch_size, dis, self.start_idx, train=True)  # !!! train=True, the only place
            # target = DataLoader(gen_samples, batch_size=self.batch_size, shuffle=True, drop_last=True)

        # ===Train===
        rewards = self.get_reward_leakgan(gen_samples, self.monte_carlo_num, dis).cpu()  # reward with MC search
        mana_loss, work_loss = self.get_adv_loss(gen_samples, rewards, dis, self.start_idx)

        return (mana_loss, work_loss)

    def init_hidden(self, batch_size=1):
        h = torch.zeros(1, batch_size, self.hidden_size)
        c = torch.zeros(1, batch_size, self.hidden_size)

        if self.use_gpu:
            return h.cuda(self.gpu_id), c.cuda(self.gpu_id)
        else:
            return h, c

    def manager_cos_loss(self, batch_size, feature_array, goal_array):
        r"""Get manager cosine distance loss

        Returns:
            cos_loss: batch_size * (seq_len / step_size)

        """
        # ===My implements===
        # offset_feature = feature_array[:, 4:, :]
        # # 不记录最后四个feature的变化
        # all_feature = feature_array[:, :-4, :]
        # all_goal = goal_array[:, :-4, :]
        # sub_feature = offset_feature - all_feature
        #
        # # L2 normalization
        # sub_feature = F.normalize(sub_feature, p=2, dim=-1)
        # all_goal = F.normalize(all_goal, p=2, dim=-1)
        #
        # cos_loss = F.cosine_similarity(sub_feature, all_goal, dim=-1)  # batch_size * (seq_len - 4)
        #
        # return cos_loss

        # ===LeakGAN origin===
        # get sub_feature and real_goal
        # batch_size, seq_len = sentences.size()
        sub_feature = torch.zeros(batch_size, (self.max_length-1) // self.step_size, self.goal_out_size)
        real_goal = torch.zeros(batch_size, (self.max_length-1) // self.step_size, self.goal_out_size)
        for i in range((self.max_length-1) // self.step_size):
            idx = i * self.step_size
            sub_feature[:, i, :] = feature_array[:, idx + self.step_size, :] - feature_array[:, idx, :]

            if i == 0:
                real_goal[:, i, :] = self.goal_init[:batch_size, :]
            else:
                idx = (i - 1) * self.step_size + 1
                real_goal[:, i, :] = torch.sum(goal_array[:, idx:idx + 4, :], dim=1)

        # L2 noramlization
        sub_feature = F.normalize(sub_feature, p=2, dim=-1)
        real_goal = F.normalize(real_goal, p=2, dim=-1)

        cos_loss = F.cosine_similarity(sub_feature, real_goal, dim=-1)

        return cos_loss

    def worker_nll_loss(self, target, leak_out_array):
        r"""Get NLL loss for worker

        Returns:
            loss: batch_size * seq_len

        """
        loss_fn = nn.NLLLoss(reduction='none',ignore_index=self.pad_idx)
        loss = loss_fn(leak_out_array.permute([0, 2, 1]), target)

        return loss

    def worker_cos_reward(self, feature_array, goal_array):
        r"""Get reward for worker (cosine distance)

        Returns:
            cos_loss: batch_size * seq_len

        """
        for i in range(int((self.max_length-1) / self.step_size)):
            real_feature = feature_array[:, i * self.step_size, :].unsqueeze(1).expand((-1, self.step_size, -1))
            feature_array[:, i * self.step_size:(i + 1) * self.step_size, :] = real_feature
            if i > 0:
                sum_goal = torch.sum(goal_array[:, (i - 1) * self.step_size:i * self.step_size, :], dim=1, keepdim=True)
            else:
                sum_goal = goal_array[:, 0, :].unsqueeze(1)
            goal_array[:, i * self.step_size:(i + 1) * self.step_size, :] = sum_goal.expand((-1, self.step_size, -1))

        offset_feature = feature_array[:, 1:, :]  # f_{t+1}, batch_size * seq_len * goal_out_size
        goal_array = goal_array[:, :self.max_length-1, :]  # batch_size * seq_len * goal_out_size
        sub_feature = offset_feature - goal_array

        # L2 normalization
        sub_feature = F.normalize(sub_feature, p=2, dim=-1)
        all_goal = F.normalize(goal_array, p=2, dim=-1)

        cos_loss = F.cosine_similarity(sub_feature, all_goal, dim=-1)  # batch_size * seq_len
        return cos_loss

    def split_params(self):
        mana_params = list()
        work_params = list()

        mana_params += list(self.manager.parameters())
        mana_params += list(self.mana2goal.parameters())
        mana_params.append(self.goal_init)

        work_params += list(self.word_embedding.parameters())
        work_params += list(self.worker.parameters())
        work_params += list(self.work2goal.parameters())
        work_params += list(self.goal2goal.parameters())

        return mana_params, work_params

    def get_reward_leakgan(self, sentences, rollout_num, dis, current_k=0):
        r"""get reward via Monte Carlo search for LeakGAN

        Args:
            sentences: size of batch_size * max_seq_len
            rollout_num:
            dis:
            current_k: current training gen

        Returns:
            reward: batch_size * (max_seq_len / step_size)

        """
        with torch.no_grad():
            batch_size = sentences.size(0)
            rewards = torch.zeros([rollout_num * ((self.max_length-1) // self.step_size), batch_size]).float()
            if self.use_gpu:
                rewards = rewards.cuda(self.gpu_id)
            idx = 0
            for i in range(rollout_num):
                for t in range((self.max_length-1) // self.step_size):
                    given_num = t * self.step_size + 1  # 1, 5, 9, ..
                    # given current words and search a complete sentence by mc
                    samples = self.rollout_mc_search_leakgan(sentences, dis, given_num)
                    out = dis(samples) # bs*2
                    out = F.softmax(out, dim=-1)
                    # using the prob of true computed by dis as the reward for current action reward
                    reward = out[:, current_k + 1] # bs
                    rewards[idx] = reward
                    idx += 1

        rewards = rewards.view(batch_size, (self.max_length-1) // self.step_size, rollout_num)
        rewards = torch.mean(rewards, dim=-1)
        return rewards

    def rollout_mc_search_leakgan(self, sentences, dis, given_num):

        batch_size, seq_len = sentences.size()

        goal_array = torch.zeros((batch_size, seq_len + 1, self.goal_out_size))

        work_hidden = self.init_hidden(batch_size)
        mana_hidden = self.init_hidden(batch_size)
        real_goal = self.goal_init[:batch_size, :]
        out = 0

        if self.use_gpu:
            goal_array = goal_array.cuda(self.gpu_id)
            real_goal = real_goal.cuda(self.gpu_id)

        # get current state
        for i in range(given_num):
            # Get feature.
            dis_inp = torch.zeros(batch_size, seq_len).long()
            dis_inp[:, :i + 1] = sentences[:, :i + 1]  # cut sentences
            leak_inp = sentences[:, i]
            if self.use_gpu:
                dis_inp = dis_inp.cuda(self.gpu_id)
                leak_inp = leak_inp.cuda(self.gpu_id)
            feature = dis.get_feature(dis_inp).unsqueeze(0)

            # Get output of one token
            # cur_goal: batch_size * 1 * goal_out_size
            out, cur_goal, work_hidden, mana_hidden = self.forward(i, leak_inp, work_hidden, mana_hidden,
                                                               feature, real_goal, train=True)

            # Save goal and update last_goal
            goal_array[:, i, :] = cur_goal.squeeze(1)
            if i > 0 and i % self.step_size == 0:
                real_goal = torch.sum(goal_array[:, i - 3:i + 1, :], dim=1)
                if i / self.step_size == 1:
                    real_goal += self.goal_init[:batch_size, :]

        samples = torch.zeros(batch_size, self.max_length-1).long()
        samples[:, :given_num] = sentences[:, :given_num]

        # MC search
        for i in range(given_num, self.max_length-1):
            # Sample one token
            out = torch.multinomial(torch.exp(out), 1).view(-1)  # [num_samples] (sampling from each row)
            samples[:, i] = out.data

            # Get feature
            dis_inp = samples
            if self.use_gpu:
                dis_inp = dis_inp.cuda(self.gpu_id)
            feature = dis.get_feature(dis_inp).unsqueeze(0)
            leak_inp = out

            # Get output of one token
            # cur_goal: batch_size * 1 * goal_out_size
            out, cur_goal, work_hidden, mana_hidden = self.forward(i, leak_inp, work_hidden, mana_hidden,
                                                               feature, real_goal, train=True)

            # Save goal and update last_goal
            goal_array[:, i, :] = cur_goal.squeeze(1)
            if i > 0 and i % self.step_size == 0:
                real_goal = torch.sum(goal_array[:, i - 3:i + 1, :], dim=1)
                if i / self.step_size == 1:
                    real_goal += self.goal_init[:batch_size, :]

        if self.use_gpu:
            samples = samples.cuda(self.gpu_id)

        return samples

    def get_adv_loss(self, target, rewards, dis, start_letter):
        r"""Returns a pseudo-loss that gives corresponding policy gradients (on calling .backward()).
        Inspired by the example in http://karpathy.github.io/2016/05/31/rl/

        Args: target, rewards, dis, start_letter
            target: batch_size * seq_len
            rewards: batch_size * seq_len (discriminator rewards for each token)

        """
        batch_size, seq_len = target.size()
        _, feature_array, goal_array, leak_out_array = self.leakgan_forward(target, dis, if_sample=False, no_log=False,
                                                                            start_letter=start_letter, train=True)

        # Manager Loss
        mana_cos_loss = self.manager_cos_loss(batch_size, feature_array, goal_array)  # batch_size * (seq_len / step_size)
        mana_loss = -torch.sum(rewards * mana_cos_loss) / (batch_size * (seq_len // self.step_size))

        # Worker Loss
        work_nll_loss = self.worker_nll_loss(target, leak_out_array)  # batch_size * seq_len
        work_cos_reward = self.worker_cos_reward(feature_array, goal_array)  # batch_size * seq_len
        work_loss = -torch.sum(work_nll_loss * work_cos_reward) / (batch_size * seq_len)

        return mana_loss, work_loss
