import os
import pipes
import socket
from subprocess import Popen, PIPE
import time
from urlparse import urlparse

from ansible.runner import Runner

from ..exceptions import BuilderError, BuilderTimeOutError, AnsibleCallError, AnsibleResponseError

from ..constants import mockchain, rsync


class Builder(object):

    def __init__(self, opts, hostname, username, job,
                 timeout, chroot, buildroot_pkgs,
                 callback,
                 remote_basedir, remote_tempdir=None,
                 macros=None, repos=None):

        # TODO: remove fields obtained from opts
        self.opts = opts
        self.hostname = hostname
        self.username = username
        self.job = job
        self.timeout = timeout
        self.chroot = chroot
        self.repos = repos or []
        self.macros = macros or {}  # rename macros to mock_ext_options
        self.callback = callback

        self.buildroot_pkgs = buildroot_pkgs or ""

        self._remote_tempdir = remote_tempdir
        self._remote_basedir = remote_basedir
        # if we're at this point we've connected and done stuff on the host
        self.conn = self._create_ans_conn()
        self.root_conn = self._create_ans_conn(username="root")

        # self.callback.log("Created builder: {}".format(self.__dict__))

        # Before use: check out the host - make sure it can build/be contacted/etc
        # self.check()

    @property
    def remote_build_dir(self):
        return self.tempdir + "/build/"

    @property
    def tempdir(self):
        if self._remote_tempdir:
            return self._remote_tempdir

        create_tmpdir_cmd = "/bin/mktemp -d {0}/{1}-XXXXX".format(
            self._remote_basedir, "mockremote")

        results = self._run_ansible(create_tmpdir_cmd)

        tempdir = None
        # TODO: use check_for_ans_error
        for _, resdict in results["contacted"].items():
            tempdir = resdict["stdout"]

        # if still nothing then we"ve broken
        if not tempdir:
            raise BuilderError("Could not make tmpdir on {0}".format(
                self.hostname))

        self._run_ansible("/bin/chmod 755 {0}".format(tempdir))
        self._remote_tempdir = tempdir

        return self._remote_tempdir

    @tempdir.setter
    def tempdir(self, value):
        self._remote_tempdir = value

    def _create_ans_conn(self, username=None):
        ans_conn = Runner(remote_user=username or self.username,
                          host_list=self.hostname + ",",
                          pattern=self.hostname,
                          forks=1,
                          transport=self.opts.ssh.transport,
                          timeout=self.timeout)
        return ans_conn

    def run_ansible_with_check(self, cmd, module_name=None, as_root=False,
                               err_codes=None, success_codes=None):

        results = self._run_ansible(cmd, module_name, as_root)

        try:
            check_for_ans_error(
                results, self.hostname, err_codes, success_codes)
        except AnsibleResponseError as response_error:
            raise AnsibleCallError(
                msg="Failed to execute ansible command",
                cmd=cmd, module_name=module_name, as_root=as_root,
                return_code=response_error.return_code,
                stdout=response_error.stdout, stderr=response_error.stderr
            )

        return results

    def _run_ansible(self, cmd, module_name=None, as_root=False):
        """
            Executes single ansible module

        :param str cmd: module command
        :param str module_name: name of the invoked module
        :param bool as_root:
        :return: ansible command result
        """
        if as_root:
            conn = self.root_conn
        else:
            conn = self.conn

        conn.module_name = module_name or "shell"
        conn.module_args = str(cmd)
        return conn.run()

    def _get_remote_pkg_dir(self, pkg):
        # the pkg will build into a dir by mockchain named:
        # $tempdir/build/results/$chroot/$packagename
        s_pkg = os.path.basename(pkg)
        pdn = s_pkg.replace(".src.rpm", "")
        remote_pkg_dir = os.path.normpath(
            os.path.join(self.remote_build_dir, "results",
                         self.chroot, pdn))

        return remote_pkg_dir

    def modify_mock_chroot_config(self):
        """
        Modify mock config for current chroot.

        Packages in buildroot_pkgs are added to minimal buildroot
        """

        if ("'{0} '".format(self.buildroot_pkgs) !=
                pipes.quote(str(self.buildroot_pkgs) + ' ')):

            # just different test if it contains only alphanumeric characters
            # allowed in packages name
            raise BuilderError("Do not try this kind of attack on me")

        self.callback.log("putting {0} into minimal buildroot of {1}"
                          .format(self.buildroot_pkgs, self.chroot))

        kwargs = {
            "chroot": self.chroot,
            "pkgs": self.buildroot_pkgs
        }
        buildroot_cmd = (
            "dest=/etc/mock/{chroot}.cfg"
            " line=\"config_opts['chroot_setup_cmd'] = 'install @buildsys-build {pkgs}'\""
            " regexp=\"^.*chroot_setup_cmd.*$\""
        )

        disable_networking_cmd = (
            "dest=/etc/mock/{chroot}.cfg"
            " line=\"config_opts['use_host_resolv'] = False\""
            " regexp=\"^.*user_host_resolv.*$\""
        )
        try:
            self.run_ansible_with_check(buildroot_cmd.format(**kwargs),
                                        module_name="lineinfile", as_root=True)
            if not self.job.enable_net:
                self.run_ansible_with_check(disable_networking_cmd.format(**kwargs),
                                            module_name="lineinfile", as_root=True)
        except BuilderError as err:
            self.callback.log(str(err))
            raise

    def collect_built_packages(self, build_details, pkg):
        self.callback.log("Listing built binary packages")
        # self.conn.module_name = "shell"

        results = self._run_ansible(
            "cd {0} && "
            "for f in `ls *.rpm |grep -v \"src.rpm$\"`; do"
            "   rpm -qp --qf \"%{{NAME}} %{{VERSION}}\n\" $f; "
            "done".format(pipes.quote(self._get_remote_pkg_dir(pkg)))
        )

        build_details["built_packages"] = list(results["contacted"].values())[0][u"stdout"]
        self.callback.log("Packages:\n{}".format(build_details["built_packages"]))

    def check_build_success(self, pkg):
        successfile = os.path.join(self._get_remote_pkg_dir(pkg), "success")
        ansible_test_results = self._run_ansible("/usr/bin/test -f {0}".format(successfile))
        check_for_ans_error(ansible_test_results, self.hostname)

    def check_if_pkg_local_or_http(self, pkg):
        """
            Local file will be sent into the build chroot,
            if pkg is a url, it will be returned as is.

            :param str pkg: path to the local file or URL
            :return str: fixed pkg location
        """
        if os.path.exists(pkg):
            dest = os.path.normpath(
                os.path.join(self.tempdir, os.path.basename(pkg)))

            self.callback.log(
                "Sending {0} to {1} to build".format(
                    os.path.basename(pkg), self.hostname))

            # FIXME should probably check this but <shrug>
            self._run_ansible("src={0} dest={1}".format(pkg, dest), module_name="copy")
        else:
            dest = pkg

        return dest

    def update_job_pkg_version(self, pkg):
        self.callback.log("Getting package information: version")
        results = self._run_ansible("rpm -qp --qf \"%{{EPOCH}}\$\$%{{VERSION}}\$\$%{{RELEASE}}\" {}".format(pkg))
        if "contacted" in results:
            # TODO:  do more sane
            raw = list(results["contacted"].values())[0][u"stdout"]
            try:
                epoch, version, release = raw.split("$$")

                if epoch == "(none)" or epoch == "0":
                    epoch = None
                if release == "(none)":
                    release = None

                self.job.pkg_main_version = version
                self.job.pkg_epoch = epoch
                self.job.pkg_release = release
            except ValueError:
                pass

    def pre_process_repo_url(self, repo_url):
        """
            Expands variables and sanitize repo url to be used for mock config
        """
        try:
            parsed_url = urlparse(repo_url)
            if parsed_url.scheme == "copr":
                user = parsed_url.netloc
                prj = parsed_url.path.split("/")[1]
                repo_url = "/".join([self.opts.results_baseurl, user, prj, self.chroot])

            else:
                if "rawhide" in self.chroot:
                    repo_url = repo_url.replace("$releasever", "rawhide")
                # custom expand variables
                repo_url = repo_url.replace("$chroot", self.chroot)
                repo_url = repo_url.replace("$distname", self.chroot.split("-")[0])

            return pipes.quote(repo_url)
        except Exception as err:
            self.callback.log("Failed not pre-process repo url: {}".format(err))
            return None

    def gen_mockchain_command(self, dest):
        buildcmd = "{0} -r {1} -l {2} ".format(
            mockchain, pipes.quote(self.chroot),
            pipes.quote(self.remote_build_dir))
        for repo in self.repos:
            repo = self.pre_process_repo_url(repo)
            if repo is not None:
                buildcmd += "-a {0} ".format(repo)

        for k, v in self.macros.items():
            mock_opt = "--define={0} {1}".format(k, v)
            buildcmd += "-m {0} ".format(pipes.quote(mock_opt))
        buildcmd += dest
        return buildcmd

    def run_command_and_wait(self, buildcmd):
        self.callback.log("executing: {0}".format(buildcmd))
        self.conn.module_name = "shell"
        self.conn.module_args = buildcmd
        _, poller = self.conn.run_async(self.timeout)
        waited = 0
        results = None
        while True:
            # TODO: try replace with ``while waited < self.timeout``
            # extract method and return waited time, raise timeout error in `else`
            results = poller.poll()

            if results["contacted"] or results["dark"]:
                break

            if waited >= self.timeout:
                msg = "Build timeout expired. Time limit: {}s, time spent: {}s".format(
                    self.timeout, waited)
                self.callback.log(msg)
                raise BuilderTimeOutError(msg)

            time.sleep(10)
            waited += 10
        return results

    def build(self, pkg):
        # build the pkg passed in
        # add pkg to various lists
        # check for success/failure of build

        # build_details = {}
        self.modify_mock_chroot_config()

        # check if pkg is local or http
        dest = self.check_if_pkg_local_or_http(pkg)

        # srpm version
        self.update_job_pkg_version(pkg)

        # construct the mockchain command
        buildcmd = self.gen_mockchain_command(dest)

        # run the mockchain command async
        ansible_build_results = self.run_command_and_wait(buildcmd)  # now raises BuildTimeoutError
        check_for_ans_error(ansible_build_results, self.hostname)  # on error raises AnsibleResponseError

        # we know the command ended successfully but not if the pkg built
        # successfully
        self.check_build_success(pkg)
        build_out = get_ans_results(ansible_build_results, self.hostname).get("stdout", "")

        build_details = {"pkg_version": self.job.pkg_version}
        self.collect_built_packages(build_details, pkg)
        return build_details, build_out

    def download(self, pkg, destdir):
            # download the pkg to destdir using rsync + ssh

        rpd = self._get_remote_pkg_dir(pkg)
        # make spaces work w/our rsync command below :(
        destdir = "'" + destdir.replace("'", "'\\''") + "'"

        # build rsync command line from the above
        remote_src = "{0}@{1}:{2}".format(self.username, self.hostname, rpd)
        ssh_opts = "'ssh -o PasswordAuthentication=no -o StrictHostKeyChecking=no'"

        rsync_log_filepath = os.path.join(destdir, "build-{}.rsync.log".format(self.job.build_id))
        command = "{} -avH -e {} {} {}/ &> {}".format(
            rsync, ssh_opts, remote_src, destdir,
            rsync_log_filepath)

        # dirty magic with Popen due to IO buffering
        # see http://thraxil.org/users/anders/posts/2008/03/13/Subprocess-Hanging-PIPE-is-your-enemy/
        # alternative: use tempfile.Tempfile as Popen stdout/stderr
        try:
            cmd = Popen(command, shell=True)
            cmd.wait()
        except Exception as error:
            raise BuilderError(msg="Failed to download from builder due to rsync error, "
                                   "see logs dir. Original error: {}".format(error))
        if cmd.returncode != 0:
            raise BuilderError(msg="Failed to download from builder due to rsync error, "
                                   "see logs dir.", return_code=cmd.returncode)

    def check(self):
        # do check of host
        try:
            socket.gethostbyname(self.hostname)
        except socket.gaierror:
            raise BuilderError("{0} could not be resolved".format(
                self.hostname))

        try:
            # check_for_ans_error(res, self.hostname)
            self.run_ansible_with_check("/bin/rpm -q mock rsync")
        except AnsibleCallError:
            raise BuilderError(msg="Build host `{0}` does not have mock or rsync installed"
                               .format(self.hostname))

        # test for path existence for mockchain and chroot config for this chroot
        try:
            self.run_ansible_with_check("/usr/bin/test -f {0}".format(mockchain))
        except AnsibleCallError:
            raise BuilderError(msg="Build host `{}` missing mockchain binary `{}`"
                               .format(self.hostname, mockchain))

        try:
            self.run_ansible_with_check("/usr/bin/test -f /etc/mock/{}.cfg"
                                        .format(self.chroot))
        except AnsibleCallError:
            raise BuilderError(msg="Build host `{}` missing mock config for chroot `{}`"
                               .format(self.hostname, self.chroot))


def get_ans_results(results, hostname):
    if hostname in results["dark"]:
        return results["dark"][hostname]
    if hostname in results["contacted"]:
        return results["contacted"][hostname]

    return {}


def check_for_ans_error(results, hostname, err_codes=None, success_codes=None):
    """
    dict includes 'msg'
    may include 'rc', 'stderr', 'stdout' and any other requested result codes

    :raises AnsibleResponseError:
    """

    if err_codes is None:
        err_codes = []
    if success_codes is None:
        success_codes = [0]

    if "dark" in results and hostname in results["dark"]:
        raise AnsibleResponseError(
            msg="Error: Could not contact/connect to {}.".format(hostname))

    error = False
    err_results = {}
    if err_codes or success_codes:
        if hostname in results["contacted"]:
            if "rc" in results["contacted"][hostname]:
                rc = int(results["contacted"][hostname]["rc"])
                err_results["return_code"] = rc
                # check for err codes first
                if rc in err_codes:
                    error = True
                    err_results["msg"] = "rc {0} matched err_codes".format(rc)
                elif rc not in success_codes:
                    error = True
                    err_results["msg"] = "rc {0} not in success_codes".format(rc)

            elif ("failed" in results["contacted"][hostname] and
                    results["contacted"][hostname]["failed"]):

                error = True
                err_results["msg"] = "results included failed as true"

        if error:
            for item in ["stdout", "stderr"]:
                if item in results["contacted"][hostname]:
                    err_results[item] = results["contacted"][hostname][item]

    if error:
        raise AnsibleResponseError(**err_results)
