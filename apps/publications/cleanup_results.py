"""Physical cleanup accounting; database work is counted separately."""

from dataclasses import dataclass


@dataclass
class CleanupResult:
    """Attempt counters, not unique paths or outstanding queue depth.

    Failed attempts are also deferred for retry. A later successful retry in
    the same pass does not erase the earlier failure. Command output reports
    outstanding durable queue records separately.
    """

    files_deleted: int = 0
    files_already_missing: int = 0
    cleanup_deferred: int = 0
    cleanup_failed: int = 0
    temporary_entries_removed: int = 0
    document_cleanup_records_processed: int = 0
    document_orphan_records_removed: int = 0

    def record_file(self, outcome):
        if outcome == "deleted":
            self.files_deleted += 1
        elif outcome == "missing":
            self.files_already_missing += 1

    def merge(self, other):
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))
