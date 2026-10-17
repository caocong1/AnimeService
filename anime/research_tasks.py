"""Shared gates for isolated premiere and episode subtitle research downloads."""
import json


def is_sample(task):
    return task.get('purpose') in ('source_sample','subtitle_sample')


def sample_episode(task):
    selected=json.loads(task['selection']).get('selected',{})
    if len(selected)!=1:raise ValueError('研究样本必须为单集')
    episode=int(next(iter(selected)))
    if episode<1 or task.get('purpose')=='source_sample' and episode!=1:
        raise ValueError('研究样本集数无效')
    return episode


def sample_table(task):
    if not is_sample(task):raise ValueError('非研究样本')
    return 'subtitle_samples' if task.get('purpose')=='subtitle_sample' else 'source_samples'


def sample_allowed(db,task):
    if not is_sample(task):return True
    if not db.review_wanted(task['show_id']):return False
    try:
        ep=sample_episode(task)
        if not db.allowed(task['show_id'],ep):return False
        if task.get('purpose')=='subtitle_sample':
            from .viewing_workflow import production_wanted,protected_sample
            if not production_wanted(db,task['show_id']) and not protected_sample(db,task):return False
            row=db.one('SELECT config FROM source_policies WHERE show_id=?',(task['show_id'],))
            return bool(db.get('subtitle_studio_enabled',False) and row and
                        json.loads(row['config']).get('mode') in ('sampling','selected','subtitle_only') and db.allowed(task['show_id'],1))
        return True
    except (ValueError,KeyError,TypeError):return False
