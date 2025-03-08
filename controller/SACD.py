import copy
import os
import torch
import numpy as np
import torch.nn.functional as F
from torch.distributions.categorical import Categorical
from utils import Double_Duel_Q_Net, Double_Q_Net, Policy_Net, ReplayBuffer


class SACD_agent:

    def __init__(self, **kwargs):
        # Init hyperparameters for agent, just like "self.gamma = opt.gamma, self.lambd = opt.lambd, ..."
        self.__dict__.update(kwargs)
        self.tau = 0.005
        self.train_counter = 0
        self.H_mean = 0
        self.replay_buffer = ReplayBuffer(
            service_shape=(self.time_steps, self.service_num, self.service_feat_dim),
            latency_shape=(self.time_steps, self.latency_feat_dim),
            dvc=self.dvc,
            buffer_size=int(1e6),
            num_actions=self.action_dim,
        )

        self.actor = Policy_Net(num_actions=self.action_dim,
                                service_feature_dim=self.service_feat_dim,
                                latency_feature_dim=self.latency_feat_dim,
                                time_steps=self.time_steps,
                                hidden_dim=self.hidden_dim,
                                fc_width=self.fc_width).to(self.dvc)

        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=self.lr)

        self.q_critic = Double_Duel_Q_Net(num_actions=self.action_dim,
                                          service_feature_dim=self.service_feat_dim,
                                          latency_feature_dim=self.latency_feat_dim,
                                          time_steps=self.time_steps,
                                          hidden_dim=self.hidden_dim,
                                          fc_width=self.fc_width).to(self.dvc)

        self.q_critic_optimizer = torch.optim.Adam(self.q_critic.parameters(), lr=self.lr)
        self.q_critic_target = copy.deepcopy(self.q_critic)
        for p in self.q_critic_target.parameters():
            p.requires_grad = False

        if self.adaptive_alpha:
            # use 0.6 because the recommended 0.98 will cause alpha explosion.
            self.target_entropy = 0.6 * (-np.log(1 / self.action_dim))  # H(discrete)>0
            self.log_alpha = torch.tensor(np.log(self.alpha), dtype=float, requires_grad=True, device=self.dvc)
            self.alpha_optim = torch.optim.Adam([self.log_alpha], lr=self.lr)

    def select_action(self, service_data, latency_data, deterministic):
        with torch.no_grad():
            # 输入预处理
            service = torch.FloatTensor(service_data).unsqueeze(0).to(self.dvc)  # [1,T,S,F]
            latency = torch.FloatTensor(latency_data).unsqueeze(0).to(self.dvc)  # [1,T,D]
            # 获取预测结果
            probs = self.actor(service, latency)

            p = self.exp_noise
            if np.random.rand() < p:
                return np.random.randint(0, self.action_dim)

            if deterministic:
                a = probs.argmax(-1).item()
            else:
                a = Categorical(probs).sample().item()
            return a

    def train(self):
        self.train_counter += 1
        (service, latency), a, r, (service_next, latency_next), dw = self.replay_buffer.sample(self.batch_size)

        # ------------------------------------------ Train Critic ----------------------------------------#
        """Compute the target soft Q value"""
        with torch.no_grad():
            next_probs = self.actor(service_next, latency_next)  # [b,a_dim]
            next_log_probs = torch.log(next_probs + 1e-8)  # [b,a_dim]
            next_q1_all, next_q2_all = self.q_critic_target(service_next, latency_next)  # [b,a_dim]
            min_next_q_all = torch.min(next_q1_all, next_q2_all)
            v_next = torch.sum(
                next_probs * (min_next_q_all - self.alpha * next_log_probs),
                dim=1,
                keepdim=True,
            )  # [b,1]
            target_Q = r + (~dw) * self.gamma * v_next
        """Update soft Q net"""
        q1_all, q2_all = self.q_critic(service, latency)  # [b,a_dim]
        q1, q2 = q1_all.gather(1, a), q2_all.gather(1, a)  # [b,1]
        q_loss = F.mse_loss(q1, target_Q) + F.mse_loss(q2, target_Q)
        self.q_critic_optimizer.zero_grad()
        q_loss.backward()

        # 梯度裁剪 可调参数
        torch.nn.utils.clip_grad_norm_(self.q_critic.parameters(), 0.5)
        self.q_critic_optimizer.step()

        # ------------------------------------------ Train Actor ----------------------------------------#
        probs = self.actor(service, latency)  # [b,a_dim]
        log_probs = torch.log(probs + 1e-8)  # [b,a_dim]

        with torch.no_grad():
            q1_all, q2_all = self.q_critic(service, latency)  # [b,a_dim]
        min_q_all = torch.min(q1_all, q2_all)

        a_loss = torch.sum(probs * (self.alpha * log_probs - min_q_all), dim=1, keepdim=False)  # [b,]

        self.actor_optimizer.zero_grad()
        a_loss.mean().backward()

        # 梯度裁剪 可调参数
        torch.nn.utils.clip_grad_norm_(self.actor.parameters(), 0.5)

        self.actor_optimizer.step()

        # ------------------------------------------ Train Alpha ----------------------------------------#
        if self.adaptive_alpha:
            with torch.no_grad():
                self.H_mean = -torch.sum(probs * log_probs, dim=1).mean()
            alpha_loss = self.log_alpha * (self.H_mean - self.target_entropy)

            self.alpha_optim.zero_grad()
            alpha_loss.backward()
            self.alpha_optim.step()

            self.alpha = self.log_alpha.exp().item()

        # ------------------------------------------ Update Target Net ----------------------------------#
        if self.train_counter % self.update_steps == 0:
            for param, target_param in zip(self.q_critic.parameters(), self.q_critic_target.parameters()):
                target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)

    def save(self, time, steps):
        save_path = f"./model/{time}/"
        if not os.path.exists(save_path):
            os.makedirs(save_path)

        torch.save(self.actor.state_dict(), f"{save_path}/sacd_actor_{time}_{steps}.pth")
        torch.save(self.q_critic.state_dict(), f"{save_path}/sacd_critic_{time}_{steps}.pth")

    def load(self, time, steps):
        save_path = f"./model/{time}/"
        self.actor.load_state_dict(torch.load(f"{save_path}/sacd_actor_{time}_{steps}.pth", map_location=self.dvc))
        self.q_critic.load_state_dict(torch.load(f"{save_path}/sacd_critic_{time}_{steps}.pth", map_location=self.dvc))
