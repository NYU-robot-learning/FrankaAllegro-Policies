# Agent implementation in fish
"""
This takes potil_vinn_offset and compute q-filter on encoder_vinn and vinn_action_qfilter
"""
import datetime
import glob
import os
import hydra
import numpy as np
import torch
import torch.nn.functional as F
import sys

from pathlib import Path

from torchvision import transforms as T

from franka_allegro.models import *
from franka_allegro.utils import *
from franka_allegro.tactile_data import *
from openteach.robot.allegro.allegro_kdl import AllegroKDL
from .agent import Agent

class TAVI(Agent):
    def __init__(self,
        data_path, expert_demo_nums, # Agent parameters
        image_out_dir, image_model_type, # Encoders
        tactile_out_dir, tactile_model_type, 
        features_repeat, # Training parameters
        experiment_name, view_num, device,
        update_critic_frequency, update_critic_target_frequency, update_actor_frequency,
        offset_mask, data_representations, # Task based offset parameters
        **kwargs
    ):
        
        # Super Agent sets the encoders, transforms and expert demonstrations
        super().__init__(
            data_path=data_path,
            expert_demo_nums=expert_demo_nums,
            image_out_dir=image_out_dir, image_model_type=image_model_type,
            tactile_out_dir=tactile_out_dir, tactile_model_type=tactile_model_type,
            view_num=view_num, device=device, update_critic_frequency=update_critic_frequency,
            features_repeat=features_repeat, experiment_name=experiment_name,
            update_critic_target_frequency=update_critic_target_frequency,
            update_actor_frequency=update_actor_frequency, 
            data_representations=data_representations, **kwargs
        )

        # NOTE All of the parameters are getting set in the agent
        self.offset_mask = torch.IntTensor(offset_mask).to(self.device)
        self.train()

    def initialize_modules(self, rl_learner_cfg, base_policy_cfg, rewarder_cfg, explorer_cfg): 
        rl_learner_cfg.action_shape = self.action_shape
        rl_learner_cfg.repr_dim = self.repr_dim(type='policy')
        self.rl_learner = hydra.utils.instantiate(
            rl_learner_cfg,
            # repr_dim = self.repr_dim(type='policy'),
            # action_shape = self.action_shape,
            actions_offset = True,
            hand_offset_scale_factor = self.hand_offset_scale_factor,
            arm_offset_scale_factor = self.arm_offset_scale_factor
        )
        self.base_policy = hydra.utils.instantiate(
            base_policy_cfg,
            expert_demos = self.expert_demos,
            tactile_repr_size = self.tactile_repr.size if 'tactile' in self.data_reprs else None,
        )
        self.rewarder = hydra.utils.instantiate(
            rewarder_cfg,
            expert_demos = self.expert_demos, 
            image_encoder = self.image_encoder if 'image'in self.data_reprs else None,
            tactile_encoder = self.tactile_encoder if 'tactile' in self.data_reprs else None
        )
        self.explorer = hydra.utils.instantiate(
            explorer_cfg
        )

    def __repr__(self):
        return f"tavi_{repr(self.rl_learner)}"
    
    def _scale_action(self, offset_action):
        if 'allegro' in self.data_reprs:
            offset_action[:,:-7] *= self.hand_offset_scale_factor
        if 'franka' in self.data_reprs or 'kinova' in self.data_reprs:
            offset_action[:,-7:] *= self.arm_offset_scale_factor

    def _print_action(self, offset_action): 
        if 'allegro' in self.data_reprs:
            print('HAND OFFSET ACTION: {}'.format(
                offset_action[:,:-7]
            ))
        if 'franka' in self.data_reprs or 'kinova' in self.data_reprs:
            print('ARM OFFSET ACTION: {}'.format(
                offset_action[:,-7:]
            ))

    def _check_limits(self, offset_action):
        # limits = [-0.1, 0.1]
        if 'allegro' in self.data_reprs:
            hand_limits = [-self.hand_offset_scale_factor-0.2, self.hand_offset_scale_factor+0.2] 
            offset_action[:,:-7] = torch.clamp(offset_action[:,:-7], min=hand_limits[0], max=hand_limits[1])
        if 'franka' in self.data_reprs or 'kinova' in self.data_reprs:
            arm_limits = [-self.arm_offset_scale_factor-0.02, self.arm_offset_scale_factor+0.02]
            offset_action[:,-7:] = torch.clamp(offset_action[:,-7:], min=arm_limits[0], max=arm_limits[1])
        return offset_action

    # Will give the next action in the step
    def base_act(self, obs, episode_step): # Returns the action for the base policy - openloop

        action, is_done = self.base_policy.act( # TODO: Make sure these are good
            obs, episode_step
        )

        return torch.FloatTensor(action).to(self.device).unsqueeze(0), is_done


    def act(self, obs, global_step, episode_step, eval_mode, metrics=None):
        with torch.no_grad():
            base_action, is_done = self.base_act(obs, episode_step)

        print('base_action.shape: {}'.format(base_action.shape))

        #noise=np.random.normal(0,0.01, size=(1, 16))[0]
        #base_action_noise=np.concatenate((np.add(base_action[:16].cpu().numpy(),noise),base_action[16:]))
        #print("base_action", base_action_noise)
        
        #base_action=np.array(base_action_noise[0]))])

        # print('base_action.shape: {}'.format(base_action_noise.shape))
        # print('base_action is {}'.format(base_action_noise))
        # base_action=torch.from_numpy(base_action_noise).float().to('cuda:1')

        with torch.no_grad():
            # Get the action image_obs
            obs = self._get_policy_reprs_from_obs( # This method is called with torch.no_grad() in training anyways
                representation_types=self.policy_representations,
                image_obs = obs['image_obs'].unsqueeze(0) / 255. if 'image' in self.data_reprs else None,
                tactile_repr = obs['tactile_repr'].unsqueeze(0) if 'tactile' in self.data_reprs else None,
                features = obs['features'].unsqueeze(0) if 'allegro' in self.data_reprs or 'franka' in self.data_reprs or 'kinova' in self.data_reprs else None,
            )

        offset_action = self.rl_learner.act(
            obs=obs, eval_mode=eval_mode, base_action=base_action,
            global_step=global_step)
        
        offset_action = self.explorer.explore(
            offset_action = offset_action,
            global_step = global_step, 
            episode_step = episode_step,
            device = self.device,
            eval_mode = eval_mode
        )

        print('offset_action: {}'.format(offset_action))

        offset_action *= self.offset_mask 
        self._scale_action(offset_action)

        # Check if the offset action is higher than the limits
        offset_action = self._check_limits(offset_action)

        self._print_action(offset_action)

        action = base_action + offset_action

        # If metrics are not None then plot the offsets
        metrics = dict()
        for i in range(len(self.offset_mask)):
            if self.offset_mask[i] == 1: # Only log the times when there is an allowed offset
                if eval_mode:
                    offset_key = f'offsets_eval/offset_{i}'
                else:
                    offset_key = f'offsets_train/offset_{i}'
                metrics[offset_key] = offset_action[:,i]

        return action.cpu().numpy()[0], base_action.cpu().numpy()[0], is_done, metrics
    
    def update(self, replay_iter, step):
        metrics = dict()

        if step % min(self.update_critic_frequency, 
                      self.update_actor_frequency,
                      self.update_critic_target_frequency) != 0:
            return metrics

        batch = next(replay_iter)
        image_obs, tactile_repr, features, action, base_action, reward, discount, next_image_obs, next_tactile_repr, next_features, base_next_action = to_torch(
            batch, self.device)
        
        # Multiply action with the offset mask just incase if the buffer was not saved that way
        offset_action = action - base_action
        offset_action *= self.offset_mask 
        action = base_action + offset_action

        # Get the representations
        # We will return none for representations that are not used in training
        obs = self._get_policy_reprs_from_obs(
            image_obs = image_obs if 'image' in self.data_reprs else None,
            tactile_repr = tactile_repr if 'tactile' in self.data_reprs else None,
            features = features if 'allegro' in self.data_reprs or 'franka' in self.data_reprs or 'kinova' in self.data_reprs else None,
            representation_types=self.policy_representations
        )

        obs_aug = None
        if self.separate_aug:
            obs_aug = self._get_policy_reprs_from_obs(
                image_obs = image_obs if 'image' in self.data_reprs else None,
                tactile_repr = tactile_repr if 'tactile' in self.data_reprs else None,
                features = features if 'allegro' in self.data_reprs or 'franka' in self.data_reprs or 'kinova' in self.data_reprs else None,
                representation_types=self.policy_representations
            )

        next_obs = self._get_policy_reprs_from_obs(
            image_obs = next_image_obs if 'image' in self.data_reprs else None, 
            tactile_repr = next_tactile_repr if 'tactile' in self.data_reprs else None,
            features = next_features if 'allegro' in self.data_reprs or 'franka' in self.data_reprs or 'kinova' in self.data_reprs else None,
            representation_types=self.policy_representations
        )

        next_obs_aug = None
        if self.separate_aug:
            next_obs_aug = self._get_policy_reprs_from_obs(
                image_obs = next_image_obs if 'image' in self.data_reprs else None, 
                tactile_repr = next_tactile_repr if 'tactile' in self.data_reprs else None,
                features = next_features if 'allegro' in self.data_reprs or 'franka' in self.data_reprs or 'kinova' in self.data_reprs else None,
                representation_types=self.policy_representations
            )

        metrics['batch_reward'] = reward.mean().item()

        if step % self.update_critic_frequency == 0:
            metrics.update(
                self.rl_learner.update_critic(
                    obs=obs,
                    obs_aug=obs_aug,
                    action=action,
                    base_next_action=base_next_action,
                    reward=reward,
                    next_obs=next_obs,
                    next_obs_aug=next_obs_aug,
                    discount=discount,
                    step=step
                )
            )

        # with torch.autograd.set_detect_anomaly(True):
        if step % self.update_actor_frequency == 0:
            metrics.update(
                self.rl_learner.update_actor(
                    obs=obs,
                    base_action=base_action,
                    step=step
                )
            )

        if step % self.update_critic_target_frequency == 0:
            self.rl_learner.update_critic_target()

        return metrics

    def get_reward(self, episode_obs, episode_id, visualize=False): # TODO: Delete the mock option
        
        final_reward, final_cost_matrix, best_expert_id = self.rewarder.get(
            obs = episode_obs
        )

        # Update the reward scale if it's the first episode
        if episode_id == 1:
            # Auto update the reward scale and get the rewards again
            self.rewarder.update_scale(current_rewards = final_reward)
            final_reward, final_cost_matrix, best_expert_id = self.rewarder.get(
                obs = episode_obs
            )

        print('final_reward: {}, best_expert_id: {}'.format(final_reward, best_expert_id))

        if visualize:
            self.plot_cost_matrix(final_cost_matrix, expert_id=best_expert_id, episode_id=episode_id)

        return final_reward
    
    def plot_cost_matrix(self, cost_matrix, expert_id, episode_id, file_name=None):
        if file_name is None:
            ts = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
            file_name = f'{ts}_expert_{expert_id}_ep_{episode_id}_cost_matrix.png'

        # Plot MxN matrix if file_name is given -> it will save the plot there if so
        cost_matrix = cost_matrix.detach().cpu().numpy()
        fig, ax = plt.subplots(figsize=(15,15),nrows=1,ncols=1)
        im = ax.matshow(cost_matrix)
        ax.set_title(f'File: {file_name}')
        fig.colorbar(im, ax=ax, label='Interactive colorbar')

        plt.xlabel('Expert Demo Timesteps')
        plt.ylabel('Observation Timesteps')
        plt.title(file_name)

        dump_dir = Path('/home/aadhithya/Workspace/tactile-learning/online_training_outs/costs') / self.experiment_name
        os.makedirs(dump_dir, exist_ok=True)
        dump_file = os.path.join(dump_dir, file_name)
        plt.savefig(dump_file, bbox_inches='tight')
        plt.close()   

    def save_snapshot(self):
        return self.rl_learner.save_snapshot()
    
    def load_snapshot(self, payload):
        return self.rl_learner.load_snapshot(payload)
    
    def load_snapshot_eval(self, payload):
        return self.rl_learner.load_snapshot_eval(payload)

    # def save_snapshot(self):
    #     keys_to_save = ['actor', 'critic', 'image_encoder']
    #     payload = {k: self.__dict__[k] for k in keys_to_save}
    #     return payload

    # def load_snapshot(self, payload):
    #     loaded_encoder = None
    #     for k, v in payload.items():
    #         self.__dict__[k] = v
    #         if k == 'image_encoder':
    #             loaded_encoder = v

    #     self.critic_target.load_state_dict(self.critic.state_dict())
    #     self.image_encoder.load_state_dict(loaded_encoder.state_dict()) # NOTE: In the actual repo they use self.vinn_encoder rather than loaded_encoder 

    #     self.image_encoder.eval()
    #     for param in self.image_encoder.parameters():
    #         param.requires_grad = False

    #     self.actor_opt = torch.optim.Adam(self.actor.parameters(), lr=self.lr)
    #     self.critic_opt = torch.optim.Adam(self.critic.parameters(), lr=self.lr)

    # def load_snapshot_eval(self, payload, bc=False):
    #     for k, v in payload.items():
    #         self.__dict__[k] = v
    #     self.critic_target.load_state_dict(self.critic.state_dict()) 

    #     self.actor_opt = torch.optim.Adam(self.actor.parameters(), lr=self.lr)
    #     self.critic_opt = torch.optim.Adam(self.critic.parameters(), lr=self.lr)