import time

from django.db import connection


class QueryCountMiddleware:
    """Adds X-DB-Queries and X-Server-Ms headers. Works with DEBUG=False."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        count = 0

        def counter(execute, sql, params, many, context):
            nonlocal count
            count += 1
            return execute(sql, params, many, context)

        started = time.perf_counter()
        with connection.execute_wrapper(counter):
            response = self.get_response(request)
        response["X-DB-Queries"] = str(count)
        response["X-Server-Ms"] = f"{(time.perf_counter() - started) * 1000:.2f}"
        return response
