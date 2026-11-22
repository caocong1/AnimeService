"""Interpret the original downloader receipt without changing its audit flags."""


def pending_receipt(task, queue):
    if not queue:
        return False
    if queue.get('lease') or queue.get('state') in ('claimed', 'review'):
        return True
    if not queue.get('committing'):
        return False
    # Thunder retains committing=1 as click history even after its original
    # reader verified the task and marked the receipt done. It is not an
    # uncertain submission once all these independent fields agree.
    reconciled = (queue.get('state') == 'done' and task.get('provider') == 'thunder'
                  and task.get('status') == 'complete' and task.get('acknowledged') == 1
                  and bool(task.get('external_id')))
    return not reconciled
