from im2scene.eval import (
    calculate_activation_statistics, calculate_frechet_distance)
from im2scene.training import (
    toggle_grad, compute_grad2, compute_bce, update_average)
from torchvision.utils import save_image, make_grid
import os
import torch
from im2scene.training import BaseTrainer
from tqdm import tqdm
import logging
from skimage.metrics import peak_signal_noise_ratio as compare_psnr
from skimage.metrics import structural_similarity as compare_ssim
import numpy as np 

logger_py = logging.getLogger(__name__)


class Trainer(BaseTrainer):
    ''' Trainer object for GIRAFFE.

    Args:
        model (nn.Module): GIRAFFE model
        optimizer (optimizer): generator optimizer object
        optimizer_d (optimizer): discriminator optimizer object
        device (device): pytorch device
        vis_dir (str): visualization directory
        multi_gpu (bool): whether to use multiple GPUs for training
        fid_dict (dict): dicionary with GT statistics for FID
        n_eval_iterations (int): number of eval iterations
        overwrite_visualization (bool): whether to overwrite
            the visualization files
    '''

    def __init__(self, model, optimizer, optimizer_d, device=None,
                 vis_dir=None,
                 val_vis_dir=None,
                 multi_gpu=False, fid_dict={},
                 n_eval_iterations=10,
                 overwrite_visualization=True, batch_size=None, recon_weight=None, **kwargs):

        self.model = model
        self.optimizer = optimizer
        self.optimizer_d = optimizer_d
        self.device = device
        self.vis_dir = vis_dir
        self.val_vis_dir = val_vis_dir
        self.multi_gpu = multi_gpu

        self.overwrite_visualization = overwrite_visualization
        self.fid_dict = fid_dict
        self.n_eval_iterations = n_eval_iterations
        self.recon_loss = torch.nn.MSELoss()
        self.vis_dict = model.generator.get_vis_dict(batch_size)
        self.recon_weight = recon_weight

        if multi_gpu:
            self.generator = torch.nn.DataParallel(self.model.generator)
            self.discriminator = torch.nn.DataParallel(
                self.model.discriminator)
            if self.model.generator_test is not None:
                self.generator_test = torch.nn.DataParallel(
                    self.model.generator_test)
            else:
                self.generator_test = None
        else:
            self.generator = self.model.generator
            self.discriminator = self.model.discriminator
            self.generator_test = self.model.generator_test

        if vis_dir is not None and not os.path.exists(vis_dir):
            os.makedirs(vis_dir)

        if val_vis_dir is not None and not os.path.exists(val_vis_dir):
            os.makedirs(val_vis_dir)

    def train_step(self, data, it=None):
        ''' Performs a training step.

        Args:
            data (dict): data dictionary
            it (int): training iteration
        '''
        loss_gen, loss_recon, loss_g_rand = self.train_step_generator(data, it)
        loss_d, reg_d, real_d, rand_d = self.train_step_discriminator(data, it)

        return {
            'generator_total': loss_gen,
            'generator_random': loss_g_rand,
            'recon': loss_recon,
            'discriminator': loss_d,
            'regularizer': reg_d,
            'real_d': real_d,
            'rand_d': rand_d,
        }

    def eval_step(self):
        ''' Performs a validation step.

        Args:
            data (dict): data dictionary
        '''

        gen = self.model.generator_test
        if gen is None:
            gen = self.model.generator
        gen.eval()

        x_fake = []
        n_iter = self.n_eval_iterations

        for i in tqdm(range(n_iter)):
            with torch.no_grad():
                x_fake.append(gen().cpu()[:, :3])
        x_fake = torch.cat(x_fake, dim=0)
        x_fake.clamp_(0., 1.)
        mu, sigma = calculate_activation_statistics(x_fake)
        fid_score = calculate_frechet_distance(
            mu, sigma, self.fid_dict['m'], self.fid_dict['s'], eps=1e-4)
        eval_dict = {
            'fid_score': fid_score
        }

        return eval_dict

    def train_step_generator(self, data, it=None, z=None):
        generator = self.generator
        discriminator = self.discriminator

        toggle_grad(generator, True)
        toggle_grad(discriminator, False)
        generator.train()
        discriminator.train()

        self.optimizer.zero_grad()

        x_real = data.get('image').to(self.device)
        if self.multi_gpu:
            latents = generator.module.get_vis_dict(x_real.shape[0])
            x_fake, _, x_rand = generator(x_real, **latents)        # pred, swap, rand
        else:
            x_fake, _, x_rand = generator(x_real)

        d_fake = discriminator(x_fake)
        d_rand = discriminator(x_rand)
        # gloss = compute_bce(d_fake, 1)
        gloss_rand = compute_bce(d_rand, 1)
        loss_recon = self.recon_loss(x_fake, x_real) * self.recon_weight
        gen_loss = gloss_rand + loss_recon 
        gen_loss.backward()
        self.optimizer.step()

        if self.generator_test is not None:
            update_average(self.generator_test, generator, beta=0.999)

        return gen_loss.item(), loss_recon.item(), gloss_rand.item()

    def train_step_discriminator(self, data, it=None, z=None):
        generator = self.generator
        discriminator = self.discriminator
        toggle_grad(generator, False)
        toggle_grad(discriminator, True)
        generator.train()
        discriminator.train()

        self.optimizer_d.zero_grad()

        x_real = data.get('image').to(self.device)
        loss_d_full = 0.

        x_real.requires_grad_()
        d_real = discriminator(x_real)

        d_loss_real = compute_bce(d_real, 1)
        loss_d_full += d_loss_real

        reg = 10. * compute_grad2(d_real, x_real).mean()
        loss_d_full += reg

        with torch.no_grad():
            if self.multi_gpu:
                latents = generator.module.get_vis_dict(batch_size=x_real.shape[0])
                x_rand = generator(x_real, **latents)[-1]
            else:
                x_rand = generator(x_real)[-1]

        x_rand.requires_grad_()
        d_rand = discriminator(x_rand)

        d_loss_rand = compute_bce(d_rand, 0)

        loss_d_full += d_loss_rand

        loss_d_full.backward()
        self.optimizer_d.step()

        d_loss = (d_loss_real + d_loss_rand)

        return (
            d_loss.item(), reg.item(), d_loss_real.item(), d_loss_rand.item())

    def record_uvs(self, uv, path, it):
        out_path = os.path.join(path, 'uv.txt')
        name_dict = {0: 'pred', 1:'swap', 2:'rand'}
        if not os.path.exists(out_path):
            f = open(out_path, 'w')
        else:
            f = open(out_path, 'a')

        for i in range(len(uv)):        # len: 3
            line = list(map(lambda x: round(x, 3), uv[i].flatten().detach().cpu().numpy().tolist()))
            out = []
            for idx in range(0, len(line)//2):
                out.append(tuple((line[2*idx], line[2*idx+1])))
            txt_line = f'{it}th {name_dict[i]}-uv : {out}\n'
            f.write(txt_line)
        f.write('\n')
        f.close()

    def visualize(self, data, it=0, mode=None, val_idx=None):
        ''' Visualized the data.

        Args:
            it (int): training iteration
        '''
        gen = self.model.generator_test
        if gen is None:
            gen = self.model.generator
        gen.eval()
        with torch.no_grad():
            # edit mira start 
            x_real = data.get('image').cuda()
            image_fake, image_swap, image_rand, uvs = self.generator(x_real, mode='val', need_uv=True)
            image_fake, image_swap, image_rand = image_fake.detach(), image_swap.detach(), image_rand.detach()
            # edit mira end 

        # edit mira start
        if mode == 'val':
            # metric values 
            psnr, ssim = 0, 0
            x_real_np = np.array(x_real.detach().cpu())
            image_fake_np = np.array(image_fake.detach().cpu())

            for idx in range(len(x_real)):
                x_real_idx = np.transpose(x_real_np[idx], (1, 2, 0))
                image_fake_idx = np.transpose(image_fake_np[idx], (1, 2, 0))
                psnr += compare_psnr(x_real_idx, image_fake_idx, data_range=1)
                ssim += compare_ssim(x_real_idx, image_fake_idx, multichannel=True, data_range=1)

            psnr, ssim = psnr/len(x_real), ssim/len(x_real)

            if val_idx == True:
                out_file_name = f'visualization_{it}_evaluation_P{round(psnr, 2)}_S{round(ssim, 2)}.png'
            else:
                return None, psnr, ssim
        else:
            out_file_name = 'visualization_%010d.png' % it
            psnr, ssim = None, None
        # edit mira end 
        image_grid = make_grid(torch.cat((x_real, image_fake.clamp_(0., 1.), image_swap.clamp_(0., 1.), image_rand.clamp_(0., 1.)), dim=0), nrow=image_fake.shape[0])
        if mode == 'val':
            save_image(image_grid, os.path.join(self.val_vis_dir, out_file_name))
            self.record_uvs(uvs, os.path.join(self.val_vis_dir), it)
        else:
            save_image(image_grid, os.path.join(self.vis_dir, out_file_name))
            self.record_uvs(uvs, os.path.join(self.vis_dir), it)
        return image_grid, psnr, ssim
