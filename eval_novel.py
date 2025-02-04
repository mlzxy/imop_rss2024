import textdistance
import warnings
warnings.filterwarnings("ignore")
from typing import List
import random
import os.path as osp
import torch
torch.set_grad_enabled(False)
import numpy as np
from tqdm import tqdm
from utils.env import rlbench_obs_config, EndEffectorPoseViaPlanning, CustomMultiTaskRLBenchEnv

from rlbench.backend.utils import task_file_to_task_class
from rlbench.action_modes.gripper_action_modes import Discrete
from rlbench.action_modes.action_mode import MoveArmThenGripper
from collections import defaultdict
from termcolor import colored
from utils import configurable, DictConfig, config_to_dict
from utils.structure import BASE_RLBENCH_TASKS, NOVEL_RLBENCH_TASKS, load_pkl, dump_pkl, ActResult
from utils.vis import *
from dataclasses import dataclass

from hdbscan import HDBSCAN

from utils.ckpt import remove_dict_prefix

import data as dlib
import utils.icp as icplib 
cat = dlib.cat

__dirname = osp.dirname(__file__)

import heuristics as heu
from network import InvariantRegionNetwork, RegionMatchingNetwork, RegionMatchingNetwork_fine


def to_np_obs(obs):
    def _get_type(x):
        if not hasattr(x, 'dtype'): return np.float32
        if x.dtype == np.float64:
            return np.float32
        return x.dtype
    return {k: np.array(v, dtype=_get_type(v)) if not isinstance(v, dict) else v for k, v in obs.items()}


def load_models(model_paths, dev: torch.device):
    m1 = RegionMatchingNetwork()
    m1.load_state_dict(remove_dict_prefix(
        torch.load(model_paths['region_match'], map_location=dev)['model'], prefix="module."))
    m1 = m1.to(dev).eval()

    m2 = InvariantRegionNetwork()
    m2.load_state_dict(remove_dict_prefix(
        torch.load(model_paths['invariant_region'], map_location='cpu')['model'], prefix="module."))
    m2 = m2.to(dev).eval()
    
    m3 = RegionMatchingNetwork_fine()
    m3.load_state_dict(remove_dict_prefix(
        torch.load(model_paths['region_match_fine'], map_location=dev)['model'], prefix="module."))
    m3 = m3.to(dev).eval()
    
    return {'region_match': m1, 'invariant_region': m2, 'region_match_fine': m3}


def get_datasets(demoset_path):
    db = dlib.RLBenchDataset(tasks=NOVEL_RLBENCH_TASKS , path=demoset_path, 
                            grid_size=0.005, min_max_pts_per_obj=5000,
                            max_episode_num=5)
    collate_fn = dlib.RLBenchCollator(use_segmap=False, training=False)
    return db, collate_fn


# ======================================================== #

@dataclass
class KeyFrame:
    type: str = "" 
    task: str = ""
    pcd = None
    rgb = None

    cluster_ids = None
    cluster_id_set = None
    assigned_cluster_id = -1   
    cluster_map = None

    key_prob_map = None
    
    item = None
    next_item = None
    
    key_region_not_found = False
    
    

def get_key_mask(kf: KeyFrame):
    if kf.key_region_not_found:
        return np.ones_like(kf.key_prob_map).astype(bool)
                                                    
    if kf.task in tasks_need_clustering:
        assert kf.assigned_cluster_id != -1
        return kf.cluster_ids == kf.assigned_cluster_id
    else:
        return kf.key_prob_map > 0.1


def smoothen_key_prob(kf: KeyFrame, neighbors=5):
    _, idxs = icplib.knn(kf.pcd, kf.pcd, k=neighbors)
    prob_map_neighbor = kf.key_prob_map[idxs.flatten()].reshape(-1, neighbors)
    kf.key_prob_map = prob_map_neighbor.max(axis=1)



def build_cluster_map(prev_kf: KeyFrame, curr_kf: KeyFrame, threshold=0.035):
    if curr_kf.task not in tasks_need_clustering: return None
    
    def compute_centers(clst, clst_map, pcd):
        centers = []
        for a in clst:
            centers.append(pcd[clst_map == a].mean(axis=0))
        return np.stack(centers)
    
    prev_centers = compute_centers(prev_kf.cluster_id_set, prev_kf.cluster_ids, prev_kf.pcd)
    curr_centers = compute_centers(curr_kf.cluster_id_set, curr_kf.cluster_ids, curr_kf.pcd)
    
    dists = np.linalg.norm(prev_centers[:, None, :] - curr_centers[None, :, :], axis=-1, ord=1)
    closest_ids = dists.argmin(axis=1).flatten()
    mapping = {}
    for a, b in enumerate(closest_ids):
        if dists[a, b] < threshold:
            mapping[a] = b
    return mapping
    

def find_most_salient_cluster(key_frame: KeyFrame, min_cluster_size=10, score_margin=0.1):
    prob_map, cluster_map, cluster_id_set = key_frame.key_prob_map, key_frame.cluster_ids, key_frame.cluster_id_set
    max_score = -1
    max_clst_id = -1
    prob_mask = prob_map > 0.1
    mean_scores = [(prob_map[prob_mask & (cluster_map == clst_id)]).mean() for clst_id in cluster_id_set]
    max_mean_score = max([v for v in mean_scores if not np.isnan(v)])
    for cid in cluster_id_set:
        mask = cluster_map == cid
        score = prob_map[mask & prob_mask].sum()
        if score > max_score and mask.sum() >= min_cluster_size and score + score_margin > max_mean_score:
            max_score = score
            max_clst_id = cid
    return max_clst_id


tasks_need_clustering = {'block_pyramid', "place_hanger_on_rack", 'lamp_on', 'phone_on_base' }
    
    
class EvaluationModelWrapper:
    def __init__(self, model_dict, db: dlib.RLBenchDataset, collate_fn, logger=print, is_novel=False,
                support_episode=-1, min_episodes_per_desc=-1, cache_to="", debug=False):
        if support_episode >= 0: logger("Warning: support_episode is SET, this shall only be set in debug")
        self.invariant_region, self.region_match, self.region_match_fine = model_dict['invariant_region'], model_dict['region_match'], model_dict['region_match_fine']
        self.db = db
        self.device = next(self.region_match_fine.parameters()).device
        self.collate = collate_fn
        self.debug = debug
        self.logger = logger
        self.is_novel = is_novel
        self.support_episode = support_episode
        if is_novel: min_episodes_per_desc = 1 # single demo 
        
        if osp.exists(cache_to):
            self.demo_db = load_pkl(cache_to)
        else:
            self.demo_db = {}
            for t in tqdm(self.db.tasks):
                self.demo_db[t] = {}
                for e in self.db.get_episodes(t):
                    kfs = self.db.get_kfs(t, e)
                    desc, vn = self.db.get_desc_and_vn(t, e)
                    if t == 'stack_blocks':
                        if '4' not in desc: continue
                    if t == 'place_cups':
                        if '3' not in desc: continue
                    if desc not in self.demo_db[t]: self.demo_db[t][desc] = []
                    self.demo_db[t][desc].append({'episode': e, 'ratio': random.random()})
                for desc in list(self.demo_db[t].keys()):
                    num_full_episodes = len([v['ratio'] for v in self.demo_db[t][desc] if v['ratio'] >= 1.0])
                    if num_full_episodes >= min_episodes_per_desc:
                        self.demo_db[t][desc] = [a for a in self.demo_db[t][desc] if a['ratio'] >= 1.0]
                    else:
                        self.demo_db[t][desc] = sorted(self.demo_db[t][desc], reverse=True, 
                                                    key=lambda x: x['ratio'])[:min_episodes_per_desc]
            if cache_to: dump_pkl(cache_to, self.demo_db)

        self.counter = defaultdict(lambda: 0)
        self._init()
    

    def _init(self):
        self.references: List[KeyFrame] = [] # store src information!
        self.pose_history = []
        self.cursor = 0

        self.last_action = None

        self.current_task = ""
        self.current_episode_description = ""
        
        self.prev_tgt_frame: KeyFrame = None
        self.color_instruction = None
        self.cluster_id_mapping = {}
        
    
    def reset(self, task, desc, color_instruction=None):
        if task != self.current_task:
            self._init()
        self._init()       
        self.color_instruction = color_instruction
        assert task is not None
        self.current_task = task
        self.current_episode_description = desc
        
        if self.support_episode < 0:
            if desc in self.demo_db[task]:
                candidates = self.demo_db[task][desc]
            else:
                demo_desc = sorted([(textdistance.levenshtein.distance(desc, demo_desc), demo_desc) for demo_desc in self.demo_db[task]])[0][1]
                self.logger(f'\t{desc} not found in existing demonstrations, use demonstrations of `{demo_desc}`')
                candidates = self.demo_db[task][demo_desc]

            ref_e = random.choice(candidates)['episode']
        else:
            ref_e = self.support_episode
        self.logger(f'\tselect support episode = {ref_e}')      

        for ind, kf in enumerate(self.db.get_kfs(task, ref_e, exclude_last=False)):
            src_t = self.db.get(task, ref_e, kf, training=False)
            src_t1 = self.db.get(task, ref_e, src_t['kf_t+1'], training=False)
            sample = {'src': {'t': src_t, 't+1': src_t1}, 
                        'tgt': { 't': src_t, 't+1': src_t1},
                        'match': None, 'index': None}
            batch = self.collate([sample, ])
            batch = dlib.to_device(batch, self.device)
            iv_region = self.invariant_region(batch, debug=False)
            prob = iv_region['output']['prob_map'].flatten().cpu().numpy()

            data = KeyFrame(type="src", task=task) 
            data.item = src_t
            data.next_item = src_t1
            data.pcd, data.rgb = src_t['pcd'], src_t['rgb']
            data.key_prob_map = prob
            src_key_mask = prob > 0.1
            
            if src_key_mask.sum() < 15:
                if ind > 0:
                    prev = self.references[ind - 1]
                    prev_src_key_mask = get_key_mask(prev)
                    if prev_src_key_mask.sum() < 15:
                        data.key_prob_map[:] = 1.0
                    else:
                        key_pcd = prev.pcd[prev_src_key_mask]
                        _, idxs = icplib.knn(key_pcd, data.pcd, k=3)
                        data.key_prob_map[:] = 0
                        data.key_prob_map[idxs.flatten()] = 1.0
                        smoothen_key_prob(data)
                else:
                    data.key_prob_map[:] = 1.0 # all activated

            if task in tasks_need_clustering:
                data.cluster_ids = HDBSCAN(min_cluster_size=15).fit_predict(src_t['pcd'])
                data.cluster_id_set = set(np.unique(data.cluster_ids).tolist()) - {-1}      
                data.assigned_cluster_id = find_most_salient_cluster(data)
                data.key_prob_map = (data.cluster_ids == data.assigned_cluster_id).astype(np.float32)

            self.references.append(data)
        
        
        if task in tasks_need_clustering:
            for i in range(len(self.references) - 1):
                d1, d2 = self.references[i], self.references[i+1]
                d2.cluster_map = build_cluster_map(d1, d2) 

        self.counter[task] += 1
        
    
    def act(self, obs):
        task = self.current_task
        self.pose_history.append(obs['gripper_pose'])
        
        if self.cursor == len(self.references) - 1: 
            if task == 'slide_cabinet_open_and_place_cups':
                self.last_action.action[-2] = 1.0
            return self.last_action
        elif self.cursor >= len(self.references):
            return None
        
        src_frame = self.references[self.cursor] 
        tgt = self.db.prepare_obs({**obs, 'task': self.current_task, 
                                                'desc': self.current_episode_description}, pose0=self.pose_history[0])
    
        tgt_frame = KeyFrame(type='tgt', task=task)
        tgt_frame.pcd, tgt_frame.rgb = tgt['pcd'], tgt['rgb']
        tgt_frame.item = tgt
        if task in tasks_need_clustering:
            tgt_frame.cluster_ids = HDBSCAN(min_cluster_size=15).fit_predict(tgt_frame.pcd)
            tgt_frame.cluster_id_set = set(np.unique(tgt_frame.cluster_ids).tolist()) - {-1}       

        sample =  {'src': {'t': src_frame.item, 't+1': src_frame.next_item}, 
                    'tgt': { 't': tgt_frame.item, 't+1': tgt_frame.item},
                    'match': None, 'index': None}
        src_frame.item['key_mask'] = get_key_mask(src_frame)
        
        key_region_propagated = False 
        if self.prev_tgt_frame is not None and task in tasks_need_clustering:
            # sometimes find the most salient cluster performs better
            tgt_frame.cluster_map =  build_cluster_map(self.prev_tgt_frame, tgt_frame) 
            
            new_cluster_id_mapping = {}
            for a, b in self.cluster_id_mapping.items():
                if a in src_frame.cluster_map and b in tgt_frame.cluster_map:
                    new_cluster_id_mapping[src_frame.cluster_map[a]] = tgt_frame.cluster_map[b]
            self.cluster_id_mapping = new_cluster_id_mapping
            
            if src_frame.assigned_cluster_id in self.cluster_id_mapping:
                tgt_frame.assigned_cluster_id = self.cluster_id_mapping[src_frame.assigned_cluster_id]
                tgt_frame.key_prob_map = (tgt_frame.cluster_ids == tgt_frame.assigned_cluster_id).astype(np.float32)
                key_region_propagated = True
            
        if not key_region_propagated:
            src_frame.item['position_mask'] = heu.get_color_position_mask(task, src_frame.item['desc'], src_frame.item['id2names'], 
                                                                        src_frame.item['rgb'], src_frame.item['mask'], **self.color_instruction)
            tgt_frame.item['position_mask'] = heu.get_color_position_mask(task, tgt_frame.item['desc'], tgt_frame.item['id2names'], 
                                                                        tgt_frame.item['rgb'], tgt_frame.item['mask'], **self.color_instruction)
            batch = self.collate([sample, ])
            batch = dlib.to_device(batch, self.device)
            matched_result = self.region_match(batch)
            prob_map = matched_result['output']['conf_matrix'].reshape(-1, len(batch['tgt']['t']['pcd'])).sum(dim=0)
            tgt_frame.key_prob_map = prob_map.flatten().cpu().numpy()
           
            smoothen_key_prob(tgt_frame) 
    
            if task in tasks_need_clustering:
                tgt_frame.assigned_cluster_id = find_most_salient_cluster(tgt_frame)
                self.cluster_id_mapping[src_frame.assigned_cluster_id] = tgt_frame.assigned_cluster_id
            else:
                if np.all(tgt_frame.key_prob_map < 0.1):
                    tgt_frame.key_region_not_found = True

        sample =  {'src': {'t': src_frame.item, 't+1': src_frame.item}, 
                'tgt': { 't': tgt_frame.item, 't+1': tgt_frame.item},
                'match': None, 'index': None}
        tgt_frame.key_region_not_found = src_frame.key_region_not_found = tgt_frame.key_region_not_found | src_frame.key_region_not_found
        
        src_frame.item['key_mask'] = get_key_mask(src_frame)
        tgt_frame.item['key_mask'] = get_key_mask(tgt_frame)
        batch = self.collate([sample, ])
        batch = dlib.to_device(batch, self.device)
        try:
            matched_result_fine = self.region_match_fine(batch)
        except Exception as e:
            self.logger(f'Exception: {e}')
            return None
        
        
        estimated_frame = matched_result_fine['output']['predict_frame'][0].cpu().numpy()
        t = estimated_frame[0]
        if t[2] < 0.75:
            estimated_frame[:, 2] = (0.75 - estimated_frame[:, 2]) + 0.75
            
        frame0 = icplib.pose7_to_frame(self.pose_history[0])
        X_02t = icplib.pose7_to_X(obs['gripper_pose']) @ icplib.inv(icplib.pose7_to_X(self.pose_history[0]))
        frame_t = icplib.h_transform(X_02t, frame0)
        X_t2tp1 = icplib.Rt_2_X(*icplib.arun(frame_t, estimated_frame))
        X_02tp1 = X_t2tp1 @ X_02t

        next_pose_X = X_02tp1 @ icplib.pose7_to_X(self.pose_history[0])
        next_pose = icplib.X_to_pose7(next_pose_X)
        
        self.cursor += 1
        if task in ['stack_blocks', 'stack_cups']: # 
            src_frame.item['ignore_col_t+1'] = False

        self.last_action = ActResult(np.array(list(next_pose) + [src_frame.item['open_t+1'], float(src_frame.item['ignore_col_t+1'])]))
        self.prev_tgt_frame = tgt_frame
        return self.last_action



def evaluate(agent: EvaluationModelWrapper, episode_length=25,
            tasks=BASE_RLBENCH_TASKS, num_episodes=5, headless=True, 
            testset_path="", 
            logger=print,
            start_episode=0):
    if isinstance(tasks, str): tasks = [tasks]
    obs_config = rlbench_obs_config(["front", "left_shoulder", "right_shoulder", "wrist"], [128, 128], method_name="")
    PCD, RGB, MASK = 0, 1, 2

    gripper_mode = Discrete()
    arm_action_mode = EndEffectorPoseViaPlanning()
    action_mode = MoveArmThenGripper(arm_action_mode, gripper_mode)

    task_classes = [task_file_to_task_class(task) for task in tasks]

    try:
        eval_env = CustomMultiTaskRLBenchEnv(
            task_classes=task_classes,
            observation_config=obs_config,
            action_mode=action_mode,
            dataset_root=testset_path,
            episode_length=episode_length,
            headless=headless,
            swap_task_every=num_episodes,
            include_lang_goal_in_obs=True
        )
        eval_env.eval = True
        eval_env.launch()
        scores = defaultdict(list)

        for task_name in tasks:
            for ep in range(start_episode, start_episode + num_episodes):
                logger(f"{task_name} - {ep}")
                episode_rollout = []
                # transitions
                obs = to_np_obs(eval_env.reset_to_demo(ep))
                lang_goal = eval_env._lang_goal
                color_information = eval_env.get_color_information()

                agent.reset(task=task_name, desc=lang_goal, color_instruction=color_information)

                for step in range(episode_length):
                    action = agent.act({**obs, 'task': task_name})
                    if action is None:
                        episode_rollout.append(0.0)
                        break
                    transition = eval_env.step(action)
                    obs = dict(transition.observation)
                    if step == episode_length - 1:
                        transition.terminal = True
                    episode_rollout.append(transition.reward)
                    if transition.terminal: break

                reward = episode_rollout[-1]
                scores[task_name].append(reward)
                txt = colored(f"\tEvaluating {task_name} | Episode {ep} | Score: {reward} | Episode Length: {len(episode_rollout)} | Lang Goal: {lang_goal}", 'red')
                logger(txt)

        for k, values in scores.items():
            logger(f'{k}, {np.mean(values):.02f}')
        mean_score = np.mean(list(scores.values()))
        logger(f'Average Score: {mean_score}')
        return scores
    finally: 
        # pass
        eval_env.shutdown()


@configurable()
def main(cfg: DictConfig):
    dev = torch.device(cfg.eval.device)
    logfile = open(osp.join(cfg.output_dir, 'log.eval.txt'), "w")
    
    if cfg.clear_output:
        import shutil
        if osp.exists('./outputs/eval_vis'):
            shutil.rmtree('./outputs/eval_vis/')

    def log(msg, printer=print):
        print(msg, file=logfile, flush=True)
        printer(msg)
    
    tasks = NOVEL_RLBENCH_TASKS
    db, collate_fn = get_datasets(cfg.demoset_path)
    model_dict = load_models(cfg.eval.model_paths, dev)

    agent = EvaluationModelWrapper(model_dict, db, collate_fn, logger=log, is_novel=True,
                                **config_to_dict(cfg.eval.agent))
    evaluate(agent,  
            tasks=tasks, 
            num_episodes=cfg.eval.episode_num, 
            headless=cfg.eval.headless, 
            testset_path=cfg.testset_path,
            logger=log,
            start_episode=cfg.eval.start_episode, 
            episode_length=cfg.eval.episode_length)

if __name__ == "__main__":
    main()
