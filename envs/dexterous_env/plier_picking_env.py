# Main script for hand interractions 
import cv2 
import gym
import numpy as np
import os
import torch
import torchvision.transforms as T

from gym import spaces
from openteach_api import DeployAPI
from openteach.robot.allegro.allegro_kdl import AllegroKDL
from openteach.utils.network import ZMQCameraSubscriber
from PIL import Image as im

from franka_allegro.tactile_data import TactileImage, TactileRepresentation
from franka_allegro.models import init_encoder_info
from franka_allegro.utils import *

from .env import DexterityEnv

class PlierPicking(DexterityEnv):
    def __init__(
        self,
        **kwargs
    ): 
        super().__init__(**kwargs)

    def set_home_state(self):
        self.home_state = dict(
            kinova = np.array( [-0.49514708,  0.29150361,  0.34026781, -0.07374543, -0.72189188, -0.04998896,  0.68624681]), 
            allegro = np.array([
                0, -0.17453293, 0.78539816, 0.78539816,           # Index
                0, -0.17453293,  0.78539816,  0.78539816,         # Middle
                0.08726646, -0.08726646, 0.87266463,  0.78539816, # Ring 
                1.04719755,  0.43633231,  0.26179939, 0.78539816  # Thumb
            ])
        )
