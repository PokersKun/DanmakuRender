from datetime import datetime
import logging
import signal
import subprocess
import sys
import os
import asyncio
import threading
import time
import multiprocessing
import queue
import glob

from DMR.message import PipeMessage
from DMR.LiveAPI import *
from os.path import join,exists
from DMR.utils import *

def isvideo(path:str) -> bool:
    ext = path.split('.')[-1]
    if ext in ['mp4','flv','ts','mkv']:
        return True
    else:
        return False

class Render():
    def __init__(self, output_dir, pipe, format, nrenders=3, engine='ffmpeg', debug=False, **kwargs) -> None:
        self.output_dir = output_dir
        self.sender = pipe
        self.format = format
        self.nrenders = int(nrenders)
        self.debug = debug
        self.engine = engine.lower()
        self.kwargs = kwargs

        self.render_queue = queue.Queue()
        self.state_dict = dict()
        self._lock = threading.Lock()

        if engine == 'ffmpeg':
            from .ffmpegrender import FFmpegRender as TargetRender
        elif engine == 'python':
            from .pythonrender import PythonRender as TargetRender
        else:
            raise NotImplementedError(f'No Render Named {engine}.')
        
        self.render_group = [TargetRender(debug=self.debug, **self.kwargs) for _ in range(self.nrenders)]

    def _distribute(self, task):
        with self._lock:
            if task == 'exit':
                for _ in range(self.nrenders):
                    self.render_queue.put(task)
                return

            group = task.get('group')
            if self.state_dict.get(group):
                self.state_dict[group].append(task)
            else:
                self.state_dict[group] = [task]
            
            if task.get('msg_type') == 'render':
                self.render_queue.put(task)

    def _gather(self, task, status, desc=''):
        with self._lock:
            group = task.get('group')

            def filter_func(x) -> bool:
                if x.get('msg_type') == 'render': return x['output'] != task['output']
                else: return bool(x)
            self.state_dict[group] = list(filter(filter_func, self.state_dict[group]))
            self.pipeSend(task['output'], type=status, desc=desc, **task)
            
            if self.state_dict[group] and self.state_dict[group][0]['msg_type'] == 'end':
                self.state_dict.pop(group)
                self.pipeSend(task.get('group'), 'end', **task)

    def pipeSend(self, msg, type='info', group=None, **kwargs):
        if self.sender:
            self.sender.put(PipeMessage('render',msg=msg,type=type,group=group,**kwargs))
        else:
            print(PipeMessage('render',msg=msg,type=type,group=group,**kwargs))

    def _render_subprocess(self, pid:int):
        this_render = self.render_group[pid]
        while not self.stoped:
            task = self.render_queue.get()
            if task == 'exit':
                self.render_queue.task_done()
                return
            
            logging.info(f'正在渲染: {task["video"]}')
            try:
                info = this_render.render_one(**task)
                if task.get('video_info'):
                    task['video_info'] = task['video_info'].copy()
                    task['video_info']['has_danmu'] = '（带弹幕版）'
                self._gather(task, 'info', desc=info)
            except KeyboardInterrupt:
                this_render.stop()
            except Exception as e:
                logging.exception(e)
                self._gather(task, 'error', desc=info)
            
            self.render_queue.task_done()

    def start(self):
        self.stoped = False
        for pid in range(self.nrenders):
            thread = threading.Thread(target=self._render_subprocess, args=(pid,), daemon=True)
            thread.start()
        return

    def add(self, video, danmaku=None, output=None, group=None, video_info=None, **kwargs):
        if video == 'end':
            self._distribute({
                'msg_type': 'end',
                'group': group,
            })
            return 
        
        if not danmaku:
            danmaku = os.path.splitext(video)[0] + '.ass'
        if not output:
            filename = os.path.splitext(os.path.basename(video))[0] + f'（带弹幕版）.{self.format}'
            if self.output_dir:
                output_dir = self.output_dir
            else:
                output_dir = os.path.dirname(video)+'（带弹幕版）'
            os.makedirs(output_dir,exist_ok=True)
            output = join(output_dir,filename)
        
        self._distribute({
            'msg_type':'render',
            'video':video,
            'danmaku':danmaku,
            'output':output,
            'group':group,
            'video_info':video_info,
            'kwargs': kwargs,
        })

    def wait(self):
        self.render_queue.join()

    def render_only(self, input_dir):
        files = glob.glob(input_dir+'/*')
        videos = [f for f in files if isvideo(f)]
        self.start()
        for vid in videos:
            danmu = os.path.splitext(vid)[0] + '.ass'
            filename = os.path.splitext(os.path.basename(vid))[0] + f'（带弹幕版）.{self.format}'
            if self.output_dir:
                output_dir = self.output_dir
            else:
                output_dir = os.path.dirname(vid)+'（带弹幕版）'
            os.makedirs(output_dir,exist_ok=True)
            output = join(output_dir,filename)
            if exists(output) and FFprobe.get_duration(output) - FFprobe.get_duration(vid) < 30:
                logging.info(f'视频 {vid} 已经存在带弹幕视频 {output}，跳过渲染.')
                continue
            if not exists(danmu):
                logging.info(f'视频 {vid} 不存在匹配的弹幕文件，跳过渲染.')
                continue

            self._distribute({
                'msg_type':'render',
                'video':vid,
                'danmaku':danmu,
                'output':output
            })
            
        self.render_queue.join()
        self.stop()

    def stop(self):
        self.stoped = True
        for proc in self.render_group:
            try:
                proc.stop()
            except Exception as e:
                logging.debug(e)
        
        self._distribute('exit')
        self.render_group.clear()
        
