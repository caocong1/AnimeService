import pytest

from anime.download_receipts import pending_receipt

TASK = {'provider': 'thunder', 'status': 'complete', 'acknowledged': 1, 'external_id': 81}
QUEUE = {'state': 'done', 'lease': '', 'committing': 1}


def test_completed_original_receipt_keeps_audit_flag_without_blocking_material():
    task, queue = TASK.copy(), QUEUE.copy()
    assert not pending_receipt(task, queue)
    assert task == TASK and queue == QUEUE


@pytest.mark.parametrize('task_patch,queue_patch', [
    ({'acknowledged': 0}, {}), ({'external_id': None}, {}),
    ({'status': 'linked'}, {}), ({'status': 'downloading'}, {}),
    ({'provider': 'qbit'}, {}), ({}, {'state': 'review'}),
    ({}, {'state': 'claimed'}), ({}, {'state': 'pending'}),
    ({}, {'lease': 'current-owner'}),
])
def test_uncertain_or_active_receipt_stays_protected(task_patch, queue_patch):
    assert pending_receipt({**TASK, **task_patch}, {**QUEUE, **queue_patch})
