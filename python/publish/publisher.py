from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import List, Any, Optional, Tuple, Mapping, Dict, Union, Callable

from github import Github, GithubException, UnknownObjectException
from github.CheckRun import CheckRun
from github.CheckRunAnnotation import CheckRunAnnotation
from github.PullRequest import PullRequest
from github.IssueComment import IssueComment

from publish import __version__, get_json_path, comment_mode_off, digest_prefix, restrict_unicode_list, \
    comment_mode_always, comment_mode_changes, comment_mode_changes_failures, comment_mode_changes_errors, \
    comment_mode_failures, comment_mode_errors, \
    get_stats_from_digest, digest_header, get_short_summary, get_long_summary_md, \
    get_long_summary_with_digest_md, get_error_annotations, get_case_annotations, get_suite_annotations, \
    get_all_tests_list_annotation, get_skipped_tests_list_annotation, get_all_tests_list, \
    get_skipped_tests_list, all_tests_list, skipped_tests_list, pull_request_build_mode_merge, \
    Annotation, SomeTestChanges
from publish import logger
from publish.github_action import GithubAction
from publish.unittestresults import UnitTestCaseResults, UnitTestRunResults, UnitTestRunDeltaResults, \
    UnitTestRunResultsOrDeltaResults, get_stats_delta, get_diff_value


@dataclass(frozen=True)
class Settings:
    token: str
    api_url: str
    graphql_url: str
    api_retries: int
    ssl_verify: bool | str
    event: dict
    event_file: Optional[str]
    event_name: str
    is_fork: bool
    repo: str
    commit: str
    json_file: Optional[str]
    json_thousands_separator: str
    json_suite_details: bool
    json_test_case_results: bool
    fail_on_errors: bool
    fail_on_failures: bool
    action_fail: bool
    action_fail_on_inconclusive: bool
    # one of these *files_glob must be set
    files_glob: Optional[str]
    junit_files_glob: Optional[str]
    nunit_files_glob: Optional[str]
    xunit_files_glob: Optional[str]
    trx_files_glob: Optional[str]
    test_file_prefix: Optional[str]
    time_factor: float
    check_name: str
    comment_title: str
    comment_mode: str
    check_run: bool
    job_summary: bool
    compare_earlier: bool
    pull_request_build: str
    test_changes_limit: int
    report_individual_runs: bool
    report_suite_out_logs: bool
    report_suite_err_logs: bool
    dedup_classes_by_file_name: bool
    large_files: bool
    ignore_runs: bool
    check_run_annotation: List[str]
    seconds_between_github_reads: float
    seconds_between_github_writes: float
    secondary_rate_limit_wait_seconds: float
    search_pull_requests: bool


@dataclasses.dataclass(frozen=True)
class PublishData:
    title: str
    summary: str
    summary_with_digest: Optional[str]
    conclusion: str
    stats: UnitTestRunResults
    stats_with_delta: Optional[UnitTestRunDeltaResults]
    before_stats: Optional[UnitTestRunResults]
    annotations: List[Annotation]
    check_url: Optional[str]
    cases: Optional[UnitTestCaseResults]

    def with_check_url(self, url: str) -> 'PublishData':
        return dataclasses.replace(self, check_url=url)

    def without_exceptions(self) -> 'PublishData':
        return dataclasses.replace(
            self,
            # remove exceptions
            stats=self.stats.without_exceptions(),
            stats_with_delta=self.stats_with_delta.without_exceptions() if self.stats_with_delta else None,
            # turn defaultdict into simple dict
            cases={test: {state: cases for state, cases in states.items()}
                   for test, states in self.cases.items()} if self.cases else None
        )

    def without_suite_details(self) -> 'PublishData':
        return dataclasses.replace(
            self,
            stats=self.stats.without_suite_details() if self.stats is not None else None,
            stats_with_delta=self.stats_with_delta.without_suite_details() if self.stats_with_delta is not None else None,
            before_stats=self.before_stats.without_suite_details() if self.before_stats is not None else None
        )

    def without_cases(self) -> 'PublishData':
        return dataclasses.replace(self, cases=None)

    def without_summary_with_digest(self) -> 'PublishData':
        return dataclasses.replace(self, summary_with_digest=None)

    @classmethod
    def _format_digit(cls, value: Union[int, Mapping[str, int], Any], thousands_separator: str) -> Union[str, Mapping[str, str], Any]:
        if isinstance(value, int):
            return f'{value:,}'.replace(',', thousands_separator)
        if isinstance(value, Mapping):
            return {k: cls._format_digit(v, thousands_separator) for (k, v) in value.items()}
        return value

    @classmethod
    def _format(cls, stats: Mapping[str, Any], thousands_separator: str) -> Dict[str, Any]:
        return {k: cls._format_digit(v, thousands_separator) for (k, v) in stats.items()}

    @classmethod
    def _formatted_stats_and_delta(cls,
                                   stats: Optional[Mapping[str, Any]],
                                   stats_with_delta: Optional[Mapping[str, Any]],
                                   before_stats: Optional[Mapping[str, Any]],
                                   thousands_separator: str) -> Mapping[str, Any]:
        d = {}
        if stats is not None:
            d.update(stats=cls._format(stats, thousands_separator))
        if stats_with_delta is not None:
            d.update(stats_with_delta=cls._format(stats_with_delta, thousands_separator))
        if before_stats is not None:
            d.update(before_stats=cls._format(before_stats, thousands_separator))
        return d

    def _as_dict(self) -> Dict[str, Any]:
        # the dict_factory removes None values
        return dataclasses.asdict(self, dict_factory=lambda x: {k: v for (k, v) in x if v is not None})

    def to_dict(self, thousands_separator: str, with_suite_details: bool, with_cases: bool) -> Mapping[str, Any]:
        data = self.without_exceptions().without_summary_with_digest()
        if not with_suite_details:
            data = data.without_suite_details()
        if not with_cases:
            data = data.without_cases()
        d = data._as_dict()

        # beautify cases, turn tuple-key into proper fields
        if d.get('cases'):
            d['cases'] = [{k: v for k, v in [('file_name', test[0]),
                                             ('class_name', test[1]),
                                             ('test_name', test[2]),
                                             ('states', states)]
                           if v}
                          for test, states in d['cases'].items()]

        # provide formatted stats and delta
        d.update(formatted=self._formatted_stats_and_delta(
            d.get('stats'), d.get('stats_with_delta'), d.get('before_stats'), thousands_separator
        ))

        return d

    def to_reduced_dict(self, thousands_separator: str) -> Mapping[str, Any]:
        # remove exceptions, suite details and cases
        data = self.without_exceptions().without_summary_with_digest().without_suite_details().without_cases()._as_dict()

        # replace some large fields with their lengths and delete individual test cases if present
        def reduce(d: Dict[str, Any]) -> Dict[str, Any]:
            d = deepcopy(d)
            if d.get('stats', {}).get('errors') is not None:
                d['stats']['errors'] = len(d['stats']['errors'])
            if d.get('before_stats', {}).get('errors') is not None:
                d['before_stats']['errors'] = len(d['before_stats']['errors'])
            if d.get('stats', {}).get('errors') is not None and \
                    d.get('before_stats', {}).get('errors') is not None and \
                    d.get('stats_with_delta', {}).get('errors') is not None:
                d['stats_with_delta']['errors'] = get_diff_value(d['stats']['errors'], d['before_stats']['errors'])
            if d.get('annotations') is not None:
                d['annotations'] = len(d['annotations'])
            return d

        data = reduce(data)
        data.update(formatted=self._formatted_stats_and_delta(
            data.get('stats'), data.get('stats_with_delta'), data.get('before_stats'), thousands_separator
        ))

        return data


class Publisher:

    def __init__(self, settings: Settings, gh: Github, gha: GithubAction):
        self._settings = settings
        self._gh = gh
        self._gha = gha
        self._repo = gh.get_repo(self._settings.repo)
        self._req = gh._Github__requester

    def publish(self,
                stats: UnitTestRunResults,
                cases: UnitTestCaseResults,
                conclusion: str):
        logger.info(f'Publishing {conclusion} results for commit {self._settings.commit}')
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f'Publishing {stats}')

        # construct publish data (test results)
        data = self.get_publish_data(stats, cases, conclusion)

        # publish the check status
        if self._settings.check_run:
            if self._settings.is_fork:
                # running on a fork, we cannot publish the check, but we can still read before_check_run
                # bump the version if you change the target of this link (if it did not exist already) or change the section
                logger.info('This action is running on a pull_request event for a fork repository. '
                            'Pull request comments and check runs cannot be created, so disabling these features. '
                            'To fully run the action on fork repository pull requests, see '
                            f'https://github.com/step-security/publish-unit-test-result-action/blob/{__version__}/README.md#support-fork-repositories-and-dependabot-branches')
            else:
                data = self.publish_check(data)

        # create data as json
        self.publish_json(data)

        # publish job summary
        if self._settings.job_summary:
            self.publish_job_summary(self._settings.comment_title, data)

        # publish pr comments
        if not self._settings.is_fork:
            if self._settings.comment_mode != comment_mode_off:
                pulls = self.get_pulls(self._settings.commit)
                if pulls:
                    for pull in pulls:
                        self.publish_comment(self._settings.comment_title, stats, pull, data.check_url, cases)
                else:
                    logger.info(f'There is no pull request for commit {self._settings.commit}')
            else:
                logger.info('Commenting on pull requests disabled')

    def get_pull_from_event(self) -> Optional[PullRequest]:
        number = get_json_path(self._settings.event, 'pull_request.number')
        repo = get_json_path(self._settings.event, 'pull_request.base.repo.full_name')
        if number is None or repo is None or repo != self._settings.repo:
            return None

        try:
            return self._repo.get_pull(number)
        except UnknownObjectException:
            return None

    def get_pulls_from_commit(self, commit: str) -> List[PullRequest]:
        try:
            # totalCount of PaginatedList calls the GitHub API just to get the total number
            # we have to retrieve them all anyway so better do this once by materialising the PaginatedList via list()
            return list(self._repo.get_commit(commit).get_pulls())
        except UnknownObjectException:
            return []

    def get_all_pulls(self, commit: str) -> List[PullRequest]:
        if self._settings.search_pull_requests:
            # totalCount of PaginatedList calls the GitHub API just to get the total number
            # we have to retrieve them all anyway so better do this once by materialising the PaginatedList via list()
            issues = list(self._gh.search_issues(f'type:pr repo:"{self._settings.repo}" {commit}'))
            pull_requests = [issue.as_pull_request() for issue in issues]
        else:
            pull_request = self.get_pull_from_event()
            pull_requests = [pull_request] if pull_request is not None else self.get_pulls_from_commit(commit)

        logger.debug(f'found {len(pull_requests)} pull requests in repo {self._settings.repo} containing commit {commit}')
        return pull_requests

    def get_pulls(self, commit: str) -> List[PullRequest]:
        # get all pull requests associated with this commit
        # TODO: simplify to event pr only, breaking change for version 3.0
        pull_requests = self.get_all_pulls(commit)

        if logger.isEnabledFor(logging.DEBUG):
            for pr in pull_requests:
                logger.debug(pr)
                logger.debug(pr.raw_data)
                logger.debug(f'PR {pr.html_url}: {pr.head.repo.full_name} -> {pr.base.repo.full_name}')

        # we can only publish the comment to PRs that are in the same repository as this action is executed in
        # so pr.base.repo.full_name must be same as GITHUB_REPOSITORY / self._settings.repo
        # we won't have permission otherwise
        pulls = list([pr
                      for pr in pull_requests
                      if pr.base.repo.full_name == self._settings.repo])

        if len(pulls) == 0:
            logger.debug(f'found no pull requests in repo {self._settings.repo} for commit {commit}')
            return []

        # we only comment on PRs that have the commit as their current head or merge commit
        pulls = [pull for pull in pulls if commit in [pull.head.sha, pull.merge_commit_sha]]
        if len(pulls) == 0:
            logger.debug(f'found no pull request in repo {self._settings.repo} with '
                         f'commit {commit} as current head or merge commit')
            return []

        # only comment on the open PRs
        pulls = [pull for pull in pulls if pull.state == 'open']
        if len(pulls) == 0:
            logger.debug(f'found multiple pull requests in repo {self._settings.repo} with '
                         f'commit {commit} as current head or merge commit but none is open')

        for pull in pulls:
            logger.debug(f'found open pull request #{pull.number} with commit {commit} as current head or merge commit')
        return pulls

    def get_stats_from_commit(self, commit_sha: str) -> Optional[UnitTestRunResults]:
        check_run = self.get_check_run(commit_sha)
        return self.get_stats_from_check_run(check_run) if check_run is not None else None

    def get_check_run(self, commit_sha: str) -> Optional[CheckRun]:
        if commit_sha is None or commit_sha == '0000000000000000000000000000000000000000':
            return None

        commit = None
        try:
            commit = self._repo.get_commit(commit_sha)
        except GithubException as e:
            if e.status == 422:
                self._gha.warning(str(e.data))
            else:
                raise e

        if commit is None:
            self._gha.error(f'Could not find commit {commit_sha}')
            return None

        runs = commit.get_check_runs()
        # totalCount calls the GitHub API, so better not do this if we are not logging the result anyway
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f'found {runs.totalCount} check runs for commit {commit_sha}')

        return self.get_check_run_from_list(list(runs))

    def get_check_run_from_list(self, runs: List[CheckRun]) -> Optional[CheckRun]:
        # filter for runs with the same name as configured
        runs = [run for run in runs if run.name == self._settings.check_name]
        logger.debug(f'there are {len(runs)} check runs with title {self._settings.check_name}')
        if len(runs) == 0:
            return None
        if len(runs) == 1:
            return runs[0]

        # filter based on summary
        runs = [run for run in runs if run.output.summary and digest_prefix in run.output.summary]
        logger.debug(f'there are {len(runs)} check runs with a test result summary')
        if len(runs) == 0:
            return None
        if len(runs) == 1:
            return runs[0]

        # filter for completed runs
        runs = [run for run in runs if run.status == 'completed']
        logger.debug(f'there are {len(runs)} check runs with completed status')
        if len(runs) == 0:
            return None
        if len(runs) == 1:
            return runs[0]

        # pick run that started latest
        return sorted(runs, key=lambda run: run.started_at, reverse=True)[0]

    @staticmethod
    def get_stats_from_check_run(check_run: CheckRun) -> Optional[UnitTestRunResults]:
        summary = check_run.output.summary
        if summary is None:
            return None
        for line in summary.split('\n'):
            logger.debug(f'summary: {line}')

        return Publisher.get_stats_from_summary_md(summary)

    @staticmethod
    def get_stats_from_summary_md(summary: str) -> Optional[UnitTestRunResults]:
        start = summary.index(digest_header) if digest_header in summary else None
        if start:
            digest = summary[start + len(digest_header):]
            end = digest.index('\n') if '\n' in digest else None
            if end:
                digest = digest[:end]
            logger.debug(f'digest: {digest}')
            stats = get_stats_from_digest(digest)
            logger.debug(f'stats: {stats}')
            return stats

    @staticmethod
    def get_test_list_from_annotation(annotation: CheckRunAnnotation) -> Optional[List[str]]:
        if annotation is None or not annotation.raw_details:
            return None
        return annotation.raw_details.split('\n')

    def get_publish_data(self,
                         stats: UnitTestRunResults,
                         cases: UnitTestCaseResults,
                         conclusion: str) -> PublishData:
        # get stats from earlier commits
        before_stats = None
        if self._settings.compare_earlier and self._settings.check_run:
            before_commit_sha = get_json_path(self._settings.event, 'before')
            logger.debug(f'comparing against before={before_commit_sha}')
            before_check_run = self.get_check_run(before_commit_sha)
            before_stats = self.get_stats_from_check_run(before_check_run) if before_check_run is not None else None
        stats_with_delta = get_stats_delta(stats, before_stats, 'earlier') if before_stats is not None else stats
        logger.debug(f'stats with delta: {stats_with_delta}')

        error_annotations = get_error_annotations(stats.errors)
        case_annotations = get_case_annotations(cases, self._settings.report_individual_runs)
        output_annotations = get_suite_annotations(stats.suite_details, self._settings.report_suite_out_logs, self._settings.report_suite_err_logs)
        test_list_annotations = self.get_test_list_annotations(cases)
        all_annotations = error_annotations + case_annotations + output_annotations + test_list_annotations

        title = get_short_summary(stats)
        summary = get_long_summary_md(stats_with_delta)
        summary_with_digest = get_long_summary_with_digest_md(stats_with_delta, stats)

        return PublishData(
            title=title,
            summary=summary,
            summary_with_digest=summary_with_digest,
            conclusion=conclusion,
            stats=stats,
            stats_with_delta=stats_with_delta if before_stats is not None else None,
            before_stats=before_stats,
            annotations=all_annotations,
            check_url=None,
            cases=cases
        )

    def publish_check(self, data: PublishData) -> PublishData:
        # we can send only 50 annotations at once, so we split them into chunks of 50
        check_run = None
        split_annotations = [annotation.to_dict() for annotation in data.annotations]
        split_annotations = [split_annotations[x:x+50] for x in range(0, len(split_annotations), 50)] or [[]]
        for annotations in split_annotations:
            output = dict(
                title=data.title,
                summary=data.summary_with_digest,
                annotations=annotations
            )

            if check_run is None:
                logger.debug(f'creating check with {len(annotations)} annotations')
                check_run = self._repo.create_check_run(name=self._settings.check_name,
                                                        head_sha=self._settings.commit,
                                                        status='completed',
                                                        conclusion=data.conclusion,
                                                        output=output)
                logger.info(f'Created check {check_run.html_url}')
            else:
                logger.debug(f'updating check with {len(annotations)} more annotations')
                check_run.edit(output=output)
                logger.debug(f'updated check')

        return data.with_check_url(check_run.html_url)

    def publish_json(self, data: PublishData):
        if self._settings.json_file:
            try:
                with open(self._settings.json_file, 'wt', encoding='utf-8') as w:
                    self.write_json(data, w, self._settings)
            except Exception as e:
                self._gha.error(f'Failed to write JSON file {self._settings.json_file}: {str(e)}')
                try:
                    os.unlink(self._settings.json_file)
                except:
                    pass

        # provide a reduced version to Github actions
        self._gha.add_to_output('json', json.dumps(data.to_reduced_dict(self._settings.json_thousands_separator), ensure_ascii=False))

    @staticmethod
    def write_json(data: PublishData, writer, settings: Settings):
        json.dump(data.to_dict(
            settings.json_thousands_separator,
            settings.json_suite_details,
            settings.json_test_case_results
        ), writer, ensure_ascii=False, indent=2)

    def publish_job_summary(self, title: str, data: PublishData):
        title = title
        stats_with_delta = data.stats_with_delta if data.stats_with_delta is not None else data.stats
        details_url = data.check_url
        summary = get_long_summary_md(stats_with_delta, details_url)
        markdown = f'## {title}\n{summary}'
        self._gha.add_to_job_summary(markdown)
        logger.info(f'Created job summary')

    @staticmethod
    def get_test_lists_from_check_run(check_run: Optional[CheckRun]) -> Tuple[Optional[List[str]], Optional[List[str]]]:
        if check_run is None:
            return None, None

        all_tests_title_regexp = re.compile(r'^\d+ test(s)? found( \(test \d+ to \d+\))?$')
        skipped_tests_title_regexp = re.compile(r'^\d+ skipped test(s)? found( \(test \d+ to \d+\))?$')

        all_tests_message_regexp = re.compile(
            r'^(There is 1 test, see "Raw output" for the name of the test)|'
            r'(There are \d+ tests, see "Raw output" for the full list of tests)|'
            r'(There are \d+ tests, see "Raw output" for the list of tests \d+ to \d+)\.$')
        skipped_tests_message_regexp = re.compile(
            r'^(There is 1 skipped test, see "Raw output" for the name of the skipped test)|'
            r'(There are \d+ skipped tests, see "Raw output" for the full list of skipped tests)|'
            r'(There are \d+ skipped tests, see "Raw output" for the list of skipped tests \d+ to \d+)\.$')

        annotations = list(check_run.get_annotations())
        all_tests_list = Publisher.get_test_list_from_annotations(annotations, all_tests_title_regexp, all_tests_message_regexp)
        skipped_tests_list = Publisher.get_test_list_from_annotations(annotations, skipped_tests_title_regexp, skipped_tests_message_regexp)

        return all_tests_list or None, skipped_tests_list or None

    @staticmethod
    def get_test_list_from_annotations(annotations: List[CheckRunAnnotation],
                                       title_regexp, message_regexp) -> List[str]:
        test_annotations: List[CheckRunAnnotation] = []

        for annotation in annotations:
            if annotation and annotation.title and annotation.message and annotation.raw_details and \
                    title_regexp.match(annotation.title) and \
                    message_regexp.match(annotation.message):
                test_annotations.append(annotation)

        test_lists = [Publisher.get_test_list_from_annotation(test_annotation)
                      for test_annotation in test_annotations]
        test_list = [test
                     for test_list in test_lists
                     if test_list
                     for test in test_list]
        return test_list

    def get_test_list_annotations(self, cases: UnitTestCaseResults, max_chunk_size: int = 64000) -> List[Annotation]:
        all_tests = get_all_tests_list_annotation(cases, max_chunk_size) \
            if all_tests_list in self._settings.check_run_annotation else []
        skipped_tests = get_skipped_tests_list_annotation(cases, max_chunk_size) \
            if skipped_tests_list in self._settings.check_run_annotation else []
        return [annotation for annotation in skipped_tests + all_tests if annotation]

    def publish_comment(self,
                        title: str,
                        stats: UnitTestRunResults,
                        pull_request: PullRequest,
                        details_url: Optional[str] = None,
                        cases: Optional[UnitTestCaseResults] = None):
        # compare them with earlier stats
        base_check_run = None
        if self._settings.compare_earlier and self._settings.check_run:
            base_commit_sha = self.get_base_commit_sha(pull_request)
            if stats.commit == base_commit_sha:
                # we do not publish a comment when we compare the commit to itself
                # that would overwrite earlier comments without change stats
                return pull_request
            logger.debug(f'comparing against base={base_commit_sha}')
            base_check_run = self.get_check_run(base_commit_sha)
        base_stats = self.get_stats_from_check_run(base_check_run) if base_check_run is not None else None
        stats_with_delta = get_stats_delta(stats, base_stats, 'base') if base_stats is not None else stats
        logger.debug(f'stats with delta: {stats_with_delta}')

        # gather test lists from check run and cases
        before_all_tests, before_skipped_tests = self.get_test_lists_from_check_run(base_check_run)
        all_tests, skipped_tests = get_all_tests_list(cases), get_skipped_tests_list(cases)
        # 'before' test names are retrieved from check runs, which have restricted unicode
        # so we have to apply the same restriction to the test names retrieved from cases, so that they match
        all_tests, skipped_tests = restrict_unicode_list(all_tests), restrict_unicode_list(skipped_tests)
        test_changes = SomeTestChanges(before_all_tests, all_tests, before_skipped_tests, skipped_tests)

        latest_comment = self.get_latest_comment(pull_request)
        latest_comment_body = latest_comment.body if latest_comment else None

        # are we required to create a comment on this PR?
        earlier_stats = self.get_stats_from_summary_md(latest_comment_body) if latest_comment_body else None
        if not self.require_comment(stats_with_delta, earlier_stats):
            logger.info(f'No pull request comment required as comment mode is {self._settings.comment_mode} (comment_mode)')
            return

        summary = get_long_summary_with_digest_md(stats_with_delta, stats, details_url, test_changes, self._settings.test_changes_limit)
        body = f'## {title}\n{summary}'

        # only create new comment none exists already
        if latest_comment is None:
            comment = pull_request.create_issue_comment(body)
            logger.info(f'Created comment for pull request #{pull_request.number}: {comment.html_url}')
        else:
            self.reuse_comment(latest_comment, body)
            logger.info(f'Edited comment for pull request #{pull_request.number}: {latest_comment.html_url}')

    def require_comment(self,
                        stats: UnitTestRunResultsOrDeltaResults,
                        earlier_stats: Optional[UnitTestRunResults]) -> bool:
        # SomeTestChanges.has_changes cannot be used here as changes between earlier comment
        # and current results cannot be identified

        if self._settings.comment_mode == comment_mode_always:
            logger.debug(f'Comment required as comment mode is {self._settings.comment_mode}')
            return True

        # helper method to detect if changes require a comment
        def do_changes_require_comment(earlier_stats_is_different_to: Optional[Callable[[UnitTestRunResultsOrDeltaResults], bool]],
                                       stats_has_changes: bool,
                                       flavour: str = '') -> bool:
            in_flavour = ''
            if flavour:
                flavour = f'{flavour} '
                in_flavour = f'in {flavour}'

            if earlier_stats is not None and earlier_stats_is_different_to(stats):
                logger.info(f'Comment required as comment mode is "{self._settings.comment_mode}" '
                            f'and {flavour}statistics are different to earlier comment')
                logger.debug(f'earlier: {earlier_stats}')
                logger.debug(f'current: {stats.without_delta() if stats.is_delta else stats}')
                return True
            if not stats.is_delta:
                logger.info(f'Comment required as comment mode is "{self._settings.comment_mode}" '
                            f'but no delta statistics to target branch available')
                return True
            if stats_has_changes:
                logger.info(f'Comment required as comment mode is "{self._settings.comment_mode}" '
                            f'and changes {in_flavour} to target branch exist')
                logger.debug(f'current: {stats}')
                return True
            return False

        if self._settings.comment_mode == comment_mode_changes and \
                do_changes_require_comment(earlier_stats.is_different if earlier_stats else None,
                                           stats.is_delta and stats.has_changes):
            return True

        if self._settings.comment_mode == comment_mode_changes_failures and \
                do_changes_require_comment(earlier_stats.is_different_in_failures if earlier_stats else None,
                                           stats.is_delta and stats.has_failure_changes,
                                           'failures'):
            return True

        if self._settings.comment_mode in [comment_mode_changes_failures, comment_mode_changes_errors] and \
                do_changes_require_comment(earlier_stats.is_different_in_errors if earlier_stats else None,
                                           stats.is_delta and stats.has_error_changes,
                                           'errors'):
            return True

        # helper method to detect if stats require a comment
        def do_stats_require_comment(earlier_stats_require: Optional[bool], stats_require: bool, flavour: str) -> bool:
            if earlier_stats is not None and earlier_stats_require:
                logger.info(f'Comment required as comment mode is {self._settings.comment_mode} '
                            f'and {flavour} existed in earlier comment')
                return True
            if stats_require:
                logger.info(f'Comment required as comment mode is {self._settings.comment_mode} '
                            f'and {flavour} exist in current comment')
                return True
            return False

        if self._settings.comment_mode == comment_mode_failures and \
                do_stats_require_comment(earlier_stats.has_failures if earlier_stats else None,
                                         stats.has_failures,
                                         'failures'):
            return True

        if self._settings.comment_mode in [comment_mode_failures, comment_mode_errors] and \
                do_stats_require_comment(earlier_stats.has_errors if earlier_stats else None,
                                         stats.has_errors,
                                         'errors'):
            return True

        return False

    def get_latest_comment(self, pull: PullRequest) -> Optional[IssueComment]:
        # get comments of this pull request
        comments = self.get_pull_request_comments(pull, order_by_updated=True)

        # get all comments that come from this action and are not hidden
        comments = self.get_action_comments(comments)

        # if there is no such comment, stop here
        if len(comments) == 0:
            return None

        # fetch latest action comment
        comment_id = comments[-1].get("databaseId")
        return pull.get_issue_comment(comment_id)

    def reuse_comment(self, comment: IssueComment, body: str):
        if ':recycle:' not in body:
            body = f'{body}\n:recycle: This comment has been updated with latest results.'

        try:
            comment.edit(body)
        except Exception as e:
            self._gha.warning(f'Failed to edit existing comment #{comment.id}')
            logger.debug('editing existing comment failed', exc_info=e)

    def get_base_commit_sha(self, pull_request: PullRequest) -> Optional[str]:
        if self._settings.pull_request_build == pull_request_build_mode_merge:
            if self._settings.event:
                # for pull request events we take the other parent of the merge commit (base)
                if self._settings.event_name == 'pull_request':
                    return get_json_path(self._settings.event, 'pull_request.base.sha')
                # for workflow run events we should take the same as for pull request events,
                # but we have no way to figure out the actual merge commit and its parents
                # we do not take the base sha from pull_request as it is not immutable
                if self._settings.event_name == 'workflow_run':
                    return None

        try:
            # we always fall back to where the branch merged off base ref
            logger.debug(f'comparing {pull_request.base.ref} with {self._settings.commit}')
            compare = self._repo.compare(pull_request.base.ref, self._settings.commit)
            return compare.merge_base_commit.sha
        except:
            logger.warning(f'could not find best common ancestor '
                           f'between base {pull_request.base.sha} '
                           f'and commit {self._settings.commit}')

        return None

    def get_pull_request_comments(self, pull: PullRequest, order_by_updated: bool) -> List[Mapping[str, Any]]:
        order = ''
        if order_by_updated:
            order = ', orderBy: { direction: ASC, field: UPDATED_AT }'

        query = dict(
            query=r'query ListComments {'
                  r'  repository(owner:"' + self._repo.owner.login + r'", name:"' + self._repo.name + r'") {'
                  r'    pullRequest(number: ' + str(pull.number) + r') {'
                  f'      comments(last: 100{order}) {{'
                  r'        nodes {'
                  r'          id, databaseId, viewerDidAuthor, body, isMinimized'
                  r'        }'
                  r'      }'
                  r'    }'
                  r'  }'
                  r'}'
        )

        headers, data = self._req.requestJsonAndCheck(
            "POST", self._settings.graphql_url, input=query
        )

        return get_json_path(data, 'data.repository.pullRequest.comments.nodes')

    def get_action_comments(self, comments: List[Mapping[str, Any]], is_minimized: Optional[bool] = False):
        comment_body_start = f'## {self._settings.comment_title}\n'
        comment_body_indicators = ['\nresults for commit ', '\nResults for commit ']
        return list([comment for comment in comments
                     if get_json_path(comment, 'viewerDidAuthor') == True
                     and (is_minimized is None or comment.get('isMinimized') == is_minimized)
                     and comment.get('body', '').startswith(comment_body_start)
                     and any(indicator in comment.get('body', '') for indicator in comment_body_indicators)])
