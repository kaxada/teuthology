import logging
import unittest.mock as mock
import os
import os.path
from pytest import raises, mark
import shutil
import subprocess
import tempfile

from teuthology.exceptions import BranchNotFoundError, CommitNotFoundError
from teuthology import repo_utils
from teuthology import parallel
repo_utils.log.setLevel(logging.WARNING)


class TestRepoUtils(object):

    @classmethod
    def setup_class(cls):
        cls.temp_path = tempfile.mkdtemp(prefix='test_repo-')
        cls.dest_path = f'{cls.temp_path}/empty_dest'
        cls.src_path = f'{cls.temp_path}/empty_src'

        if 'TEST_ONLINE' in os.environ:
            cls.repo_url = 'https://github.com/ceph/empty.git'
            cls.commit = '71245d8e454a06a38a00bff09d8f19607c72e8bf'
        else:
            cls.repo_url = f'file://{cls.src_path}'
            cls.commit = None

    @classmethod
    def teardown_class(cls):
        shutil.rmtree(cls.temp_path)

    def setup_method(self, method):
        assert not os.path.exists(self.dest_path)
        proc = subprocess.Popen(
            ('git', 'init', self.src_path),
            stdout=subprocess.PIPE,
        )
        assert proc.wait() == 0
        proc = subprocess.Popen(
            ('git', 'config', 'user.email', 'test@ceph.com'),
            cwd=self.src_path,
            stdout=subprocess.PIPE,
        )
        assert proc.wait() == 0
        proc = subprocess.Popen(
            ('git', 'config', 'user.name', 'Test User'),
            cwd=self.src_path,
            stdout=subprocess.PIPE,
        )
        assert proc.wait() == 0
        proc = subprocess.Popen(
            ('git', 'commit', '--allow-empty', '--allow-empty-message',
             '--no-edit'),
            cwd=self.src_path,
            stdout=subprocess.PIPE,
        )
        assert proc.wait() == 0
        if not self.commit:
            result = subprocess.check_output(
                'git rev-parse HEAD',
                shell=True,
                cwd=self.src_path,
            ).split()
            assert result
            self.commit = result[0].decode()

    def teardown_method(self, method):
        shutil.rmtree(self.dest_path, ignore_errors=True)

    def test_clone_repo_existing_branch(self):
        repo_utils.clone_repo(self.repo_url, self.dest_path, 'master', self.commit)
        assert os.path.exists(self.dest_path)

    def test_clone_repo_non_existing_branch(self):
        with raises(BranchNotFoundError):
            repo_utils.clone_repo(self.repo_url, self.dest_path, 'nobranch', self.commit)
        assert not os.path.exists(self.dest_path)

    def test_fetch_no_repo(self):
        fake_dest_path = f'{self.temp_path}/not_a_repo'
        assert not os.path.exists(fake_dest_path)
        with raises(OSError):
            repo_utils.fetch(fake_dest_path)
        assert not os.path.exists(fake_dest_path)

    def test_fetch_noop(self):
        repo_utils.clone_repo(self.repo_url, self.dest_path, 'master', self.commit)
        repo_utils.fetch(self.dest_path)
        assert os.path.exists(self.dest_path)

    def test_fetch_branch_no_repo(self):
        fake_dest_path = f'{self.temp_path}/not_a_repo'
        assert not os.path.exists(fake_dest_path)
        with raises(OSError):
            repo_utils.fetch_branch(fake_dest_path, 'master')
        assert not os.path.exists(fake_dest_path)

    def test_fetch_branch_fake_branch(self):
        repo_utils.clone_repo(self.repo_url, self.dest_path, 'master', self.commit)
        with raises(BranchNotFoundError):
            repo_utils.fetch_branch(self.dest_path, 'nobranch')

    @mark.parametrize('git_str',
                      ["fatal: couldn't find remote ref",
                       "fatal: Couldn't find remote ref"])
    @mock.patch('subprocess.Popen')
    def test_fetch_branch_different_git_versions(self, mock_popen, git_str):
        """
        Newer git versions return a lower case string
        See: https://github.com/git/git/commit/0b9c3afdbfb629363
        """
        branch_name = 'nobranch'
        process_mock = mock.Mock()
        attrs = {
            'wait.return_value': 1,
            'stdout.read.return_value': f"{git_str} {branch_name}".encode(),
        }
        process_mock.configure_mock(**attrs)
        mock_popen.return_value = process_mock
        with raises(BranchNotFoundError):
            repo_utils.fetch_branch('', branch_name)

    def test_enforce_existing_branch(self):
        repo_utils.enforce_repo_state(self.repo_url, self.dest_path,
                                      'master')
        assert os.path.exists(self.dest_path)

    def test_enforce_existing_commit(self):
        repo_utils.enforce_repo_state(self.repo_url, self.dest_path,
                                      'master', self.commit)
        assert os.path.exists(self.dest_path)

    def test_enforce_non_existing_branch(self):
        with raises(BranchNotFoundError):
            repo_utils.enforce_repo_state(self.repo_url, self.dest_path,
                                          'blah', self.commit)
        assert not os.path.exists(self.dest_path)

    def test_enforce_non_existing_commit(self):
        with raises(CommitNotFoundError):
            repo_utils.enforce_repo_state(self.repo_url, self.dest_path,
                                          'master', 'c69e90807d222c1719c45c8c758bf6fac3d985f1')
        assert not os.path.exists(self.dest_path)

    def test_enforce_multiple_calls_same_branch(self):
        repo_utils.enforce_repo_state(self.repo_url, self.dest_path,
                                      'master', self.commit)
        assert os.path.exists(self.dest_path)
        repo_utils.enforce_repo_state(self.repo_url, self.dest_path,
                                      'master', self.commit)
        assert os.path.exists(self.dest_path)
        repo_utils.enforce_repo_state(self.repo_url, self.dest_path,
                                      'master', self.commit)
        assert os.path.exists(self.dest_path)

    def test_enforce_multiple_calls_different_branches(self):
        with raises(BranchNotFoundError):
            repo_utils.enforce_repo_state(self.repo_url, self.dest_path,
                                          'blah1')
        assert not os.path.exists(self.dest_path)
        repo_utils.enforce_repo_state(self.repo_url, self.dest_path,
                                      'master', self.commit)
        assert os.path.exists(self.dest_path)
        repo_utils.enforce_repo_state(self.repo_url, self.dest_path,
                                      'master', self.commit)
        assert os.path.exists(self.dest_path)
        with raises(BranchNotFoundError):
            repo_utils.enforce_repo_state(self.repo_url, self.dest_path,
                                          'blah2')
        assert not os.path.exists(self.dest_path)
        repo_utils.enforce_repo_state(self.repo_url, self.dest_path,
                                      'master', self.commit)
        assert os.path.exists(self.dest_path)

    def test_enforce_invalid_branch(self):
        with raises(ValueError):
            repo_utils.enforce_repo_state(self.repo_url, self.dest_path, 'a b', self.commit)

    def test_simultaneous_access(self):
        count = 5
        with parallel.parallel() as p:
            for _ in range(count):
                p.spawn(repo_utils.enforce_repo_state, self.repo_url,
                        self.dest_path, 'master', self.commit)
            for result in p:
                assert result is None

    def test_simultaneous_access_different_branches(self):
        branches = [('master', self.commit),  ('master', self.commit), ('nobranch', 'nocommit'),
                    ('nobranch', 'nocommit'), ('master', self.commit), ('nobranch', 'nocommit')]

        with parallel.parallel() as p:
            for branch, commit in branches:
                if branch == 'master':
                    p.spawn(repo_utils.enforce_repo_state, self.repo_url,
                            self.dest_path, branch, commit)
                else:
                    dest_path = f'{self.dest_path}_{branch}'

                    def func():
                        repo_utils.enforce_repo_state(
                            self.repo_url, dest_path,
                            branch, commit)

                    p.spawn(
                        raises,
                        BranchNotFoundError,
                        func,
                    )
            for result in p:
                pass

    URLS_AND_DIRNAMES = [
        ('git@git.ceph.com/ceph-qa-suite.git', 'git.ceph.com_ceph-qa-suite'),
        ('git://git.ceph.com/ceph-qa-suite.git', 'git.ceph.com_ceph-qa-suite'),
        ('https://github.com/ceph/ceph', 'github.com_ceph_ceph'),
        ('https://github.com/liewegas/ceph.git', 'github.com_liewegas_ceph'),
        ('file:///my/dir/has/ceph.git', 'my_dir_has_ceph'),
    ]

    @mark.parametrize("input_, expected", URLS_AND_DIRNAMES)
    def test_url_to_dirname(self, input_, expected):
        assert repo_utils.url_to_dirname(input_) == expected
