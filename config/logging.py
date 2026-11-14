"""Keep activation proofs and query strings out of ordinary request logs."""

import logging
import re


class RedactRequestProofs(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        message = re.sub(r"(/accounts/activate/)[^\s\"']+", r"\1[redacted]/", message)
        message = re.sub(r"(\b(?:GET|POST|HEAD) /[^\s?]*)\?[^\s\"]+", r"\1?[redacted]", message)
        record.msg = message
        record.args = ()
        return True
