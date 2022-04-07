"""工作流程控制
"""

from PyMongoWrapper import F
from jindai import  PipelineStage, Pipeline, Task
from jindai.helpers import execute_query_expr
from jindai.models import TaskDBO, parser


class FlowControlStage(PipelineStage):
    """Base class for flow control pipeline stages"""

    def __init__(self) -> None:
        self._next = None
        self._pipelines = [getattr(self, a) for a in dir(self) if isinstance(getattr(self, a), Pipeline)]
        super().__init__()

    @property
    def logger(self):
        """Logger"""
        return lambda *x: self._logger(self.__class__.__name__, '|', *x)

    @logger.setter
    def logger(self, val):
        self._logger = val
        for pipeline in self._pipelines:
            pipeline.logger = val
            
    @property
    def next(self):
        """Next stage in pipeline"""
        return self._next
    
    @next.setter
    def next(self, val):
        self._next = val
        for pipeline in self._pipelines:
            if pipeline.stages:
                pipeline.stages[-1].next = val
    

class RepeatWhile(FlowControlStage):
    """重复"""

    def __init__(self, pipeline, times=1, cond=''):
        """
        Args:
            pipeline (pipeline): 要重复执行的流程
            times (int): 重复的次数
            cond (QUERY): 重复的条件
        """
        self.times = times
        self.times_key = f'REPEATWHILE_{id(self)}_TIMES_COUNTER'
        self.cond = parser.eval(cond if cond else f'{self.times_key} < {times}')
        self.pipeline = Pipeline(pipeline, self.logger)
        super().__init__()

    def flow(self, paragraph):
        if paragraph[self.times_key] is None:
            paragraph[self.times_key] = 0
        
        flag = execute_query_expr(self.cond, paragraph)
        paragraph[self.times_key] += 1
        
        if flag and self.pipeline.stages:
            self.pipeline.stages[-1].next = self
            yield paragraph, self.pipeline.stages[0]
        else:
            paragraph[self.times_key] = None
            yield paragraph, self.next


class Condition(FlowControlStage):
    """条件判断"""

    def __init__(self, cond, iftrue, iffalse):
        """
        Args:
            cond (QUERY): 检查的条件
            iftrue (pipeline): 条件成立时执行的流程
            iffalse (pipeline): 条件不成立时执行的流程
        """
        self.cond = parser.eval(cond)
        self.iftrue = Pipeline(iftrue, self.logger)
        self.iffalse = Pipeline(iffalse, self.logger)
        super().__init__()
    
    def flow(self, paragraph):
        pipeline = self.iftrue
        if not execute_query_expr(self.cond, paragraph):
            pipeline = self.iffalse
        if pipeline.stages:
            yield paragraph, pipeline.stages[0]
        else:
            yield paragraph, self.next


class CallTask(FlowControlStage):
    """调用其他任务（流程）"""

    def __init__(self, task, pipeline_only=False, params=''):
        """
        Args:
            task (TASK): 任务ID
            pipeline_only (bool): 仅调用任务中的处理流程，若为 false，则于 summarize 阶段完整调用该任务
            params (QUERY): 设置任务中各数据源和流程参数
        """
        t = TaskDBO.first(F.id == task)
        assert t, '指定的任务不存在'
        self.pipeline_only = pipeline_only
        if params:
            params = parser.eval(params)
            for k, v in params.items():
                secs = k.split('.')
                target = t.pipeline
                for sec in secs[1:-1]:
                    if sec.isnumeric():
                        assert isinstance(target, list) and len(target) > int(sec), '请指定正确的下标，从0开始'
                        target = target[int(sec)][1]
                    else:
                        assert sec in target, '不存在该参数'
                        target = target[sec]
                        if isinstance(target, list) and len(target) == 2 and isinstance(target[0], str) and isinstance(target[1], dict):
                            target = target[1]
                sec = secs[-1]
                target[sec] = v
        
        self._pipelines = []
        self.task = Task.from_dbo(t)
        if self.pipeline_only:
            self.pipeline = self.task.pipeline
        
        super().__init__()
        
    def flow(self, paragraph):
        if self.pipeline_only and self.pipeline.stages:
            yield paragraph, self.pipeline.stages[0]
        else:
            yield paragraph, self.next
    
    def summarize(self, _):
        if self.pipeline_only:
            return self.pipeline.summarize()
        else:
            return self.task.execute()
