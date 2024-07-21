import torch, time, os
import numpy as np
import torch.nn as nn
import torch.optim as optim
from torchvision.datasets import MNIST
from torchvision import transforms
from torch.utils.data import DataLoader
from torchvision.utils import save_image
import torch.nn.functional as F
import time

from runx.logx import logx
import argparse

# U-Net模型定义
from unet import Unet
from utils import *


# DDPM定义
class DDPM(nn.Module):
    def __init__(self, model, betas, n_T, device):
        super(DDPM, self).__init__()
        self.model = model.to(device)

        # register_buffer 可以提前保存alpha相关，节约时间
        for k, v in self.ddpm_schedules(betas[0], betas[1], n_T).items():
            self.register_buffer(k, v)

        self.n_T = n_T
        self.device = device
        self.loss_mse = nn.MSELoss()

    def ddpm_schedules(self, beta1, beta2, T):
        '''
        提前计算各个step的alpha，这里beta是线性变化
        :param beta1: beta的下限
        :param beta2: beta的下限
        :param T: 总共的step数
        '''
        assert beta1 < beta2 < 1.0, "beta1 and beta2 must be in (0, 1)"

        beta_t = (beta2 - beta1) * torch.arange(0, T + 1, dtype=torch.float32) / T + beta1  # 生成beta1-beta2均匀分布的数组
        sqrt_beta_t = torch.sqrt(beta_t)
        alpha_t = 1 - beta_t
        log_alpha_t = torch.log(alpha_t)
        alphabar_t = torch.cumsum(log_alpha_t, dim=0).exp()  # alpha累乘

        sqrtab = torch.sqrt(alphabar_t)  # 根号alpha累乘
        oneover_sqrta = 1 / torch.sqrt(alpha_t)  # 1 / 根号alpha

        sqrtmab = torch.sqrt(1 - alphabar_t)  # 根号下（1-alpha累乘）
        mab_over_sqrtmab_inv = (1 - alpha_t) / sqrtmab

        return {
            "alpha_t": alpha_t,  # \alpha_t
            "oneover_sqrta": oneover_sqrta,  # 1/\sqrt{\alpha_t}
            "sqrt_beta_t": sqrt_beta_t,  # \sqrt{\beta_t}
            "alphabar_t": alphabar_t,  # \bar{\alpha_t}
            "sqrtab": sqrtab,  # \sqrt{\bar{\alpha_t}} # 加噪标准差
            "sqrtmab": sqrtmab,  # \sqrt{1-\bar{\alpha_t}}  # 加噪均值
            "mab_over_sqrtmab": mab_over_sqrtmab_inv,  # (1-\alpha_t)/\sqrt{1-\bar{\alpha_t}}
        }

    def forward(self, x):
        """
        训练过程中, 随机选择step和生成噪声
        """
        # 随机选择step
        _ts = torch.randint(1, self.n_T + 1, (x.shape[0],)).to(self.device)  # t ~ Uniform(0, n_T)
        # 随机生成正态分布噪声
        noise = torch.randn_like(x)  # eps ~ N(0, 1)
        # 加噪后的图像x_t
        x_t = (
                self.sqrtab[_ts, None, None, None] * x
                + self.sqrtmab[_ts, None, None, None] * noise

        )

        # 将unet预测的对应step的正态分布噪声与真实噪声做对比
        return self.loss_mse(noise, self.model(x_t, _ts / self.n_T))

    def sample(self, n_sample, size, device):
        # 随机生成初始噪声图片 x_T ~ N(0, 1)
        x_i = torch.randn(n_sample, *size).to(device)
        for i in range(self.n_T, 0, -1):
            t_is = torch.tensor([i / self.n_T]).to(device)
            t_is = t_is.repeat(n_sample, 1, 1, 1)

            z = torch.randn(n_sample, *size).to(device) if i > 1 else 0

            eps = self.model(x_i, t_is)
            x_i = x_i[:n_sample]
            x_i = self.oneover_sqrta[i] * (x_i - eps * self.mab_over_sqrtmab[i]) + self.sqrt_beta_t[i] * z
        return x_i


# 训练过程和推理过程
class ImageGenerator(object):
    def __init__(self, lr=1e-4, n_T=400):
        '''
        初始化，定义超参数、数据集、网络结构等
        '''
        self.epoch = 20
        self.sample_num = 100
        self.batch_size = 256
        self.lr = lr
        self.n_T = n_T
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.init_dataloader()
        self.sampler = DDPM(model=Unet(in_channels=1), betas=(1e-4, 0.02), n_T=self.n_T, device=self.device).to(
            self.device)
        self.optimizer = optim.Adam(self.sampler.model.parameters(), lr=self.lr)

    def init_dataloader(self):
        '''
        初始化数据集和dataloader
        '''
        tf = transforms.Compose([
            transforms.ToTensor(),
        ])
        train_dataset = MNIST('./data/',
                              train=True,
                              download=True,
                              transform=tf)
        self.train_dataloader = DataLoader(train_dataset, batch_size=self.batch_size, shuffle=True, drop_last=True)
        val_dataset = MNIST('./data/',
                            train=False,
                            download=True,
                            transform=tf)
        self.val_dataloader = DataLoader(val_dataset, batch_size=self.batch_size, shuffle=False)

    def train(self, output_path='results/Diffusion'):
        self.sampler.train()
        print('start training...')
        for epoch in range(self.epoch):
            start_time = time.time()
            self.sampler.model.train()
            loss_mean = 0
            for i, (images, labels) in enumerate(self.train_dataloader):
                images, labels = images.to(self.device), labels.to(self.device)

                # 将latent和condition拼接后输入网络
                loss = self.sampler(images)
                loss_mean += loss.item()
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()

            end_time = time.time()
            epoch_mins, epoch_secs = epoch_time(start_time, end_time)

            train_loss = loss_mean / len(self.train_dataloader)
            print('epoch:{}, loss:{:.4f}, time cost:{}min {:.2f}s'.format(epoch, train_loss, epoch_mins, epoch_secs))
            self.visualize_results(epoch, output_path)

    @torch.no_grad()
    def visualize_results(self, epoch, output_path):
        self.sampler.eval()
        # 保存结果路径
        os.makedirs(output_path, exist_ok=True)

        tot_num_samples = self.sample_num
        image_frame_dim = int(np.floor(np.sqrt(tot_num_samples)))
        out = self.sampler.sample(tot_num_samples, (1, 28, 28), self.device)
        save_image(out, os.path.join(output_path, f'{epoch:02}.jpg'), nrow=image_frame_dim)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='DDPM')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--log_dir', type=str, default='./log')
    parser.add_argument('--result_dir', type=str, default='./result')
    parser.add_argument(
        '--action', type=int, default=0,
        help="0 = single test"
             "1 = self.lr"
             "2 = self.n_T'"
    )
    args = parser.parse_args()

    logx.initialize(logdir=args.log_dir, coolname=False, tensorboard=False)

    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    torch.backends.cudnn.deterministic = True  # 保证每次结果一样

    if args.action == 0:
        generator = ImageGenerator()
        generator.train()
    elif args.action == 1:
        lr_list = [1e-6, 1e-5, 1e-4, 1e-3]
        for lr in lr_list:
            generator = ImageGenerator(lr=lr)
            output_path = os.path.join(args.result_dir, f'lr{lr}')
            generator.train(output_path=output_path)
    elif args.action == 2:
        T_list = [100, 300, 500, 700, 900]
        for T_n in T_list:
            generator = ImageGenerator(T_n=T_n)
            output_path = os.path.join(args.result_dir, f'lr{lr}')
            generator.train(output_path=output_path)