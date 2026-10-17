"""Submission gates, including explicitly authorized, audited UI-fault fallback."""
from .source_blacklist import source_blocker
from .native_indexes import task_clean_blocker

def fallback_receipt(db,t):
    receipt=db.get('qbit_fallback_'+t['hash'],{}) if t else {}
    return bool(db.get('qbit_fallback_enabled',False) and t and t.get('provider')=='qbit' and
                receipt.get('hash')==t['hash'] and receipt.get('show_id')==t['show_id'] and
                receipt.get('reason') and receipt.get('thunder_absent'))

THUNDER_PENDING = '迅雷优先；自动接入尚待验证，已暂停自动提交。不会直接改用qB；需先确认迅雷任务失败且已停止，才能交接备用下载器。'


class Refusal(ValueError):
    """A submission gate said no for a stated reason; the text is ours and safe to record and show."""
    safe_message=True


def submission_blocker(db):
    if db.get('downloader_preference', 'qbit') == 'thunder_first' and not db.get('thunder_ui_enabled',False):
        # No verified Thunder writer/stop adapter exists yet. A KV readiness flag
        # must not accidentally bypass the guard and send the work to qB.
        return THUNDER_PENDING
    return ''


def require_submission_ready(db,task=None):
    if task:
        blocked=source_blocker(db,task['title'],task['selection'])
        if blocked:raise Refusal(blocked)
        blocked=task_clean_blocker(db,task)
        if blocked:raise Refusal(blocked)
    if task and fallback_receipt(db,task):return
    reason = submission_blocker(db)
    if reason:
        raise Refusal(reason)


def status(db):
    preference = db.get('downloader_preference', 'qbit')
    return {'preference': preference,
            'label': '迅雷优先，qB备用' if preference == 'thunder_first' else 'qB优先，迅雷仅在确实失败后考虑',
            'ready': not bool(submission_blocker(db)),
            'blocker': submission_blocker(db),
            'qbit_profile': 'D:\\MediaService\\qbit-profile',
            'profile_note': '本机后台持续查源；迅雷界面任务由定时 computer use 执行。已确认下载失败或 Computer Use 故障时，经归属和未提交核查后自动交接 qB 备用；不重投结果不确定的任务。' if preference=='thunder_first' and db.get('thunder_ui_enabled',False) else '使用现有127.0.0.1:4870 qB；不自动启动或切换实例，不定时操作桌面。只有qB实际下载失败后才考虑迅雷；已有和不确定任务继续对账，不重复下载。'}
