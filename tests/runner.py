"""Standard unittest discovery, complete sharding, and durable per-test evidence."""
import argparse
import faulthandler
import json
from pathlib import Path
import sys
import time
import unittest


def test_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_cases(item)
        else:
            yield item


def shard(suite, index, count):
    if count < 1 or not 0 <= index < count:
        raise ValueError("Shard index must be >= 0 and < shard count")
    cases = sorted(test_cases(suite), key=lambda case: case.id())
    ids = [case.id() for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Discovery found duplicate test IDs")
    return unittest.TestSuite(cases[index::count])


class Result(unittest.TextTestResult):
    def __init__(self, *args, report=None, watchdog=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.report = report
        self.durations = []
        self.watchdog = watchdog
        self.outcome = "passed"

    def event(self, **record):
        if self.report:
            self.report.write(json.dumps(record) + "\n")
            self.report.flush()

    def startTest(self, test):
        if self.watchdog:
            faulthandler.dump_traceback_later(120, repeat=True)
        self.started = time.monotonic()
        self.outcome = "passed"
        self.event(event="start", test=test.id())
        super().startTest(test)

    def addError(self, test, err):
        self.outcome = "error"
        self.event(event="error", test=test.id(), traceback=self._exc_info_to_string(err, test))
        super().addError(test, err)

    def addFailure(self, test, err):
        self.outcome = "failed"
        self.event(event="failure", test=test.id(), traceback=self._exc_info_to_string(err, test))
        super().addFailure(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.outcome = "failed"
            self.event(event="subtest_failure", test=test.id(), subtest=str(subtest),
                       traceback=self._exc_info_to_string(err, test))
        super().addSubTest(test, subtest, err)

    def addSkip(self, test, reason):
        if self.outcome == "passed":
            self.outcome = "skipped"
        self.event(event="skip", test=test.id(), reason=reason)
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self.outcome = "expected_failure"
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self.outcome = "unexpected_success"
        super().addUnexpectedSuccess(test)

    def stopTest(self, test):
        if self.watchdog:
            faulthandler.cancel_dump_traceback_later()
        elapsed = time.monotonic() - self.started
        self.durations.append((elapsed, test.id()))
        self.event(event="finish", test=test.id(), outcome=self.outcome, seconds=elapsed)
        super().stopTest(test)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="Optional unittest module, class or test IDs")
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--report", type=Path, help="Flush JSONL results after every test event")
    parser.add_argument("--list", action="store_true", help="Print selected test IDs without running")
    args = parser.parse_args(argv)
    loader = unittest.TestLoader()
    suite = (loader.loadTestsFromNames(args.tests) if args.tests else
             loader.discover(str(Path(__file__).parent), top_level_dir=str(Path(__file__).parent.parent)))
    # Discovery/import failures must fail every shard, never disappear into another partition.
    if loader.errors:
        print("\n".join(loader.errors), file=sys.stderr)
        return 1
    try:
        selected = shard(suite, args.shard_index, args.shard_count)
    except ValueError as error:
        parser.error(str(error))
    if not selected.countTestCases():
        parser.error("No tests selected")
    if args.list:
        print("\n".join(test.id() for test in test_cases(selected)))
        return 0
    report = None
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        report = args.report.open("w", encoding="utf-8")
    try:
        faulthandler.enable()
        runner = unittest.TextTestRunner(verbosity=2, resultclass=lambda *a, **kw: Result(*a, report=report, watchdog=True, **kw))
        result = runner.run(selected)
        result.event(event="summary", tests=result.testsRun, successful=result.wasSuccessful(),
                     failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
                     expected_failures=len(result.expectedFailures), unexpected_successes=len(result.unexpectedSuccesses))
        print("\nSlowest tests (including setup and cleanup):", file=sys.stderr)
        for elapsed, name in sorted(result.durations, reverse=True)[:10]:
            print("%8.2fs  %s" % (elapsed, name), file=sys.stderr)
        return 0 if result.wasSuccessful() else 1
    finally:
        faulthandler.cancel_dump_traceback_later()
        if report:
            report.close()


if __name__ == "__main__":
    raise SystemExit(main())
