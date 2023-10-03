import json
import logging
import os
import random
import re
import subprocess
import time
import tempfile

from subprocess import CalledProcessError

from teuthology import misc

from teuthology.openstack import OpenStack, OpenStackInstance
from teuthology.config import config
from teuthology.contextutil import safe_while
from teuthology.exceptions import QuotaExceededError


log = logging.getLogger(__name__)


class ProvisionOpenStack(OpenStack):
    """
    A class that provides methods for creating and destroying virtual machine
    instances using OpenStack
    """
    def __init__(self):
        super(ProvisionOpenStack, self).__init__()
        self.user_data = tempfile.mktemp()
        log.debug(f"ProvisionOpenStack: {str(config.openstack)}")
        self.basename = 'target'
        self.up_string = 'The system is finally up'
        self.property = "%16x" % random.getrandbits(128)

    def __del__(self):
        if os.path.exists(self.user_data):
            os.unlink(self.user_data)

    def init_user_data(self, os_type, os_version):
        """
        Get the user-data file that is fit for os_type and os_version.
        It is responsible for setting up enough for ansible to take
        over.
        """
        template_path = config['openstack']['user-data'].format(
            os_type=os_type,
            os_version=os_version)
        nameserver = config['openstack'].get('nameserver', '8.8.8.8')
        user_data_template = open(template_path).read()
        user_data = user_data_template.format(
            up=self.up_string,
            nameserver=nameserver,
            username=self.username,
            lab_domain=config.lab_domain)
        open(self.user_data, 'w').write(user_data)

    def _openstack(self, subcommand, get=None):
        # do not use OpenStack().run because its
        # bugous for volume create as of openstackclient 3.2.0
        # https://bugs.launchpad.net/python-openstackclient/+bug/1619726
        #r = OpenStack().run("%s -f json " % command)
        json_result = misc.sh(f"openstack {subcommand} -f json")
        r = json.loads(json_result)
        return self.get_value(r, get) if get else r

    def _create_volume(self, volume_name, size):
        """
        Create a volume and return valume id
        """
        volume_id = None
        try:
            volume_id = self._openstack(f"volume show {volume_name}", 'id')
        except subprocess.CalledProcessError as e:
            if 'No volume with a name or ID' not in e.output:
                raise e
        if volume_id:
            log.warning(
                f"Volume {volume_name} already exists with ID {volume_id}; using it"
            )
        if not (
            volume_id := self._openstack(
                f"volume create {config['openstack'].get('volume-create', '')}"
                + f" --property ownedby={config['openstack']['ip']}"
                + f" --size {str(size)}"
                + ' '
                + volume_name,
                'id',
            )
        ):
            raise Exception(f"Failed to create volume {volume_name}")
        log.info(f"Volume {volume_name} created with ID {volume_id}")
        return volume_id

    def _await_volume_status(self, volume_id, status='available'):
        """
        Wait for volume to have status, like 'available' or 'in-use'
        """
        with safe_while(sleep=4, tries=50, action=f"volume {volume_id}") as proceed:
            while proceed():
                try:
                    volume_status = self._openstack(f"volume show {volume_id}", 'status')
                    if volume_status == status:
                        break
                    else:
                        log.debug(f"volume {volume_id} not in '{status}' status yet")
                except subprocess.CalledProcessError:
                    log.warning(f"volume {volume_id} not information available yet")

    def _attach_volume(self, volume_id, name):
        """
        Attach volume to OpenStack instance.

        Try and attach volume to server, wait until volume gets in-use state.
        """
        with safe_while(sleep=20, increment=20, tries=3, action=f"add volume {volume_id}") as proceed:
            while proceed():
                try:
                    misc.sh(f"openstack server add volume {name} {volume_id}")
                    break
                except subprocess.CalledProcessError:
                    log.warning("openstack add volume failed unexpectedly; retrying")
        self._await_volume_status(volume_id, 'in-use')

    def attach_volumes(self, server_name, volumes):
        """
        Create and attach volumes to the named OpenStack instance.
        If attachment is failed, make another try.
        """
        for i in range(volumes['count']):
            volume_name = f'{server_name}-{str(i)}'
            volume_id = None
            with safe_while(sleep=10, tries=3, action=f"volume {volume_name}") as proceed:
                while proceed():
                    try:
                        volume_id = self._create_volume(volume_name, volumes['size'])
                        self._await_volume_status(volume_id, 'available')
                        self._attach_volume(volume_id, server_name)
                        break
                    except Exception as e:
                        log.warning(f"{e}")
                        if volume_id:
                            OpenStack().volume_delete(volume_id)

    @staticmethod
    def ip2name(prefix, ip):
        """
        return the instance name suffixed with the IP address.
        """
        digits = map(int, re.findall('(\d+)\.(\d+)\.(\d+)\.(\d+)', ip)[0])
        return prefix + "%03d%03d%03d%03d" % tuple(digits)

    def create(self, num, os_type, os_version, arch, resources_hint):
        """
        Create num OpenStack instances running os_type os_version and
        return their names. Each instance has at least the resources
        described in resources_hint.
        """
        log.debug('ProvisionOpenStack:create')
        if arch is None:
            arch = self.get_default_arch()
        resources_hint = self.interpret_hints({
            'machine': config['openstack']['machine'],
            'volumes': config['openstack']['volumes'],
        }, resources_hint)
        self.init_user_data(os_type, os_version)
        image = self.image(os_type, os_version, arch)
        if 'network' in config['openstack']:
            net = "--nic net-id=" + str(self.net_id(config['openstack']['network']))
        else:
            net = ''
        flavor = self.flavor(resources_hint['machine'], arch)
        worker_group = config['openstack']['worker_group'] or 'teuthology-worker'
        keypair = config['openstack']['keypair'] or 'teuthology'
        cmd = (
            "flock --close --timeout 28800 /tmp/teuthology-server-create.lock"
            + " openstack server create"
            + " "
            + config['openstack'].get('server-create', '')
            + " -f json "
            + " --image '"
            + str(image)
            + "'"
            + " --flavor '"
            + str(flavor)
            + "'"
            + f" --key-name {keypair} "
            + " --user-data "
            + str(self.user_data)
            + " "
            + net
            + " --min "
            + str(num)
            + " --max "
            + str(num)
            + f" --security-group {worker_group}"
            + " --property teuthology="
            + self.property
            + " --property ownedby="
            + config.openstack['ip']
            + " --wait "
            + " "
            + self.basename
        )
        try:
            self.run(cmd, type='compute')
        except CalledProcessError as exc:
            if "quota exceeded" in exc.output.lower():
                raise QuotaExceededError(message=exc.output)
            raise
        instances = filter(
            lambda instance: self.property in instance['Properties'],
            self.list_instances())
        instances = [OpenStackInstance(i['ID']) for i in instances]
        fqdns = []
        try:
            network = config['openstack'].get('network', '')
            for instance in instances:
                ip = instance.get_ip(network)
                name = self.ip2name(self.basename, ip)
                self.run("server set " +
                         "--name " + name + " " +
                         instance['ID'])
                fqdn = f'{name}.{config.lab_domain}'
                if not misc.ssh_keyscan_wait(fqdn):
                    console_log = misc.sh(f"openstack console log show {instance['ID']} || true")
                    log.error(console_log)
                    raise ValueError(f'ssh_keyscan_wait failed for {fqdn}')
                time.sleep(15)
                if not self.cloud_init_wait(instance):
                    raise ValueError(f'cloud_init_wait failed for {fqdn}')
                self.attach_volumes(name, resources_hint['volumes'])
                fqdns.append(fqdn)
        except Exception as e:
            log.exception(str(e))
            for id in [instance['ID'] for instance in instances]:
                self.destroy(id)
            raise e
        return fqdns

    def destroy(self, name_or_id):
        log.debug(f'ProvisionOpenStack:destroy {name_or_id}')
        return OpenStackInstance(name_or_id).destroy()
