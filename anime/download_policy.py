"""Downloader preference is a submission gate, never an implicit fallback."""

THUNDER_PENDING = '迅雷优先；自动接入尚待验证，已暂停自动提交。不会直接改用qB；需先确认迅雷任务失败且已停止，才能交接备用下载器。'


def submission_blocker(db):
    if db.get('downloader_preference', 'qbit') == 'thunder_first' and not db.get('thunder_ui_enabled',False):
        # No verified Thunder writer/stop adapter exists yet. A KV readiness flag
        # must not accidentally bypass the guard and send the work to qB.
        return THUNDER_PENDING
    return ''


def require_submission_ready(db):
    reason = submission_blocker(db)
    if reason:
        raise ValueError(reason)


def status(db):
    preference = db.get('downloader_preference', 'qbit')
    return {'preference': preference,
            'label': '迅雷优先，qB备用' if preference == 'thunder_first' else '独立qB配置',
            'ready': not bool(submission_blocker(db)),
            'blocker': submission_blocker(db),
            'qbit_profile': 'D:\\MediaService\\qbit-profile',
            'profile_note': '本机后台持续查源；迅雷界面任务由定时 computer use 执行，锁屏时排队，解锁后补执行。qB只作已确认失败后的备用。' if preference=='thunder_first' and db.get('thunder_ui_enabled',False) else '专用配置与日常默认qB的任务列表独立；没有迁移或清空默认历史。'}
