"""File responses that stop reading the disk once the client has gone.

Starlette's FileResponse reads and sends the whole byte range without ever calling receive(),
and uvicorn's send() silently drops data after a disconnect. An aborted open-ended range on a
full episode (every seek in a browser or on the TV) therefore keeps reading to EOF, and keeps a
graceful stop waiting for it. These subclasses use the pattern of Starlette's StreamingResponse:
the response runs next to a receive() loop and both stop when either side finishes. Headers,
status, ETag and range handling stay in the parent class. A background task on such a response
would also be cancelled on disconnect; none of the media routes uses one.
"""
import anyio
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


class MediaFileResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await super().__call__(scope, receive, send)
        try:
            async with anyio.create_task_group() as group:
                async def disconnected():
                    while (await receive())['type'] != 'http.disconnect':
                        pass
                    group.cancel_scope.cancel()
                group.start_soon(disconnected)
                await super().__call__(scope, receive, send)
                group.cancel_scope.cancel()
        except BaseExceptionGroup as errors:
            # Keep FileResponse's own errors (e.g. a missing file) unwrapped, as before.
            if len(errors.exceptions) != 1:
                raise
            error = errors.exceptions[0]
            raise error from error.__cause__ or (None if error.__suppress_context__ else error.__context__)


class MediaStaticFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if type(response) is not FileResponse:
            return response  # NotModifiedResponse and anything else stay as they are
        return MediaFileResponse(full_path, status_code=status_code, stat_result=stat_result)
