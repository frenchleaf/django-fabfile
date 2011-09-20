"""Check :doc:`README` or :class:`django_fabfile.utils.Config` docstring
for setup instructions."""

from collections import defaultdict
from ConfigParser import SafeConfigParser
from contextlib import contextmanager
from datetime import datetime
from itertools import chain
from json import loads
import logging
import os
import re
from time import sleep
from traceback import format_exc

from boto import BotoConfigLocations, connect_ec2
from boto.ec2 import regions
from boto.exception import EC2ResponseError
from fabric.api import sudo, task
from fabric.contrib.files import exists
from pkg_resources import resource_stream

from django_fabfile import __name__ as pkg_name


logger = logging.getLogger(__name__)


def timestamp():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S')


class Config(object):

    """Make use from Django settings or local config file.

    Django settings will be checked out if environment variable
    `DJANGO_SETTINGS_MODULE` configured properly. If not configured
    within Django settings, then options will be taken from
    ./fabfile.cfg file - copy-paste rows that should be overriden from
    :download:`django_fabfile/fabfile.cfg.def
    <../django_fabfile/fabfile.cfg.def>`."""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(Config, cls).__new__(cls, *args, **kwargs)
        return cls._instance

    def __init__(self):
        self.fabfile = SafeConfigParser()
        self.fabfile.read(BotoConfigLocations)
        self.fabfile.readfp(resource_stream(pkg_name, 'fabfile.cfg.def'))
        self.fabfile.read('fabfile.cfg')

    def get_from_django(self, option, section='DEFAULT'):
        if os.environ.get('DJANGO_SETTINGS_MODULE'):
            try:
                from django.conf import settings
                return settings.FABFILE[section][option]
            except:
                pass

    def get(self, section, option):
        return (self.get_from_django(option, section) or
                self.fabfile.get(section, option))

    def getboolean(self, section, option):
        return (self.get_from_django(option, section) or
                self.fabfile.getboolean(section, option))

    def getfloat(self, section, option):
        return (self.get_from_django(option, section) or
                self.fabfile.getfloat(section, option))

    def getint(self, section, option):
        return (self.get_from_django(option, section) or
                self.fabfile.getint(section, option))

    def get_creds(self):
        return dict(
            aws_access_key_id=self.get('Credentials', 'AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=self.get('Credentials',
                                           'AWS_SECRET_ACCESS_KEY'))


config = Config()


def get_region_conn(region_name=None):
    """Connect to partially spelled `region_name`.

    Return connection to default boto region if called without
    arguments.

    :param region_name: may be spelled partially."""
    creds = config.get_creds()
    if region_name:
        matched = [reg for reg in regions(**creds) if re.match(region_name,
                                                               reg.name)]
        assert len(matched) > 0, 'No region matches {0}'.format(region_name)
        assert len(matched) == 1, 'Several regions matches {0}'.format(
            region_name)
        return matched[0].connect(**creds)
    else:
        return connect_ec2(**creds)


class StateNotChangedError(Exception):

    def __init__(self, state):
        self.state = state

    def __str__(self):
        return 'State remain {0} after limited time gone'.format(self.state)


def wait_for(obj, state, attrs=None, max_sleep=30, limit=5 * 60):
    """Wait for attribute to go into state.

    :param attrs: nested attribute names.
    :type attrs: list"""

    def get_state(obj, attrs=None):
        obj_state = obj.update()
        if not attrs:
            return obj_state
        else:
            attr = obj
            for attr_name in attrs:
                attr = getattr(attr, attr_name)
            return attr
    logger.debug('Calling {0} updates'.format(obj))
    for i in range(10):     # Resource may be reported as "not exists"
        try:                # right after creation.
            obj_state = get_state(obj, attrs)
        except Exception as err:
            logger.debug(str(err))
            sleep(10)
        else:
            break
    logger.debug('Called {0} update'.format(obj))
    obj_region = getattr(obj, 'region', None)
    logger.debug('State fetched from {0} in {1}'.format(obj, obj_region))
    if obj_state != state:
        if obj_region:
            info = 'Waiting for the {obj} in {obj.region} to be {state}...'
        else:
            info = 'Waiting for the {obj} to be {state}...'
        logger.info(info.format(obj=obj, state=state))
        slept, sleep_for = 0, 3
        while obj_state != state and slept < limit:
            logger.info('still {0}...'.format(obj_state))
            sleep_for = sleep_for + 5 if sleep_for < max_sleep else max_sleep
            sleep(sleep_for)
            slept += sleep_for
            obj_state = get_state(obj, attrs)
        if obj_state == state:
            logger.info('done.')
        else:
            raise StateNotChangedError(obj_state)


class WaitForProper(object):

    """Decorate consecutive exceptions eating.

    >>> @WaitForProper(attempts=3, pause=5)
    ... def test():
    ...     1 / 0
    ...
    >>> test()
    ZeroDivisionError('integer division or modulo by zero',)
     waiting next 5 sec (2 times left)
    ZeroDivisionError('integer division or modulo by zero',)
     waiting next 5 sec (1 times left)
    ZeroDivisionError('integer division or modulo by zero',)
    """

    def __init__(self, attempts=10, pause=10):
        self.attempts = attempts
        self.pause = pause

    def __call__(self, func):

        def wrapper(*args, **kwargs):
            attempts = self.attempts
            while attempts > 0:
                attempts -= 1
                try:
                    return func(*args, **kwargs)
                except BaseException as err:
                    logger.debug(format_exc())
                    logger.error(repr(err))

                    if attempts > 0:
                        logger.info('waiting next {0} sec ({1} times left)'
                            .format(self.pause, attempts))
                        sleep(self.pause)
                else:
                    break
        return wrapper

ssh_timeout_attempts = config.getint('DEFAULT', 'SSH_TIMEOUT_ATTEMPTS')
ssh_timeout_interval = config.getint('DEFAULT', 'SSH_TIMEOUT_INTERVAL')
wait_for_exists = WaitForProper(attempts=ssh_timeout_attempts,
                                pause=ssh_timeout_interval)(exists)
wait_for_sudo = WaitForProper(attempts=ssh_timeout_attempts,
                              pause=ssh_timeout_interval)(sudo)


def add_tags(res, tags):
    for tag in tags:
        if tags[tag]:
            res.add_tag(tag, tags[tag])
    logger.debug('Tags added to {0}'.format(res))


def get_descr_attr(resource, attr):
    try:
        return loads(resource.description)[attr]
    except:
        pass


def get_snap_vol(snap):
    return get_descr_attr(snap, 'Volume') or snap.volume_id


def get_snap_instance(snap):
    return get_descr_attr(snap, 'Instance')


def get_snap_device(snap):
    return get_descr_attr(snap, 'Device')


def get_snap_time(snap):
    for format_ in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f'):
        try:
            return datetime.strptime(get_descr_attr(snap, 'Time'), format_)
        except (TypeError, ValueError):
            continue
    # Use attribute if can't parse description.
    return datetime.strptime(snap.start_time, '%Y-%m-%dT%H:%M:%S.000Z')


def get_inst_by_id(region, instance_id):
    res = get_region_conn(region.name).get_all_instances([instance_id, ])
    assert len(res) == 1, (
        'Returned more than 1 {0} for instance_id {1}'.format(res,
                                                      instance_id))
    instances = res[0].instances
    assert len(instances) == 1, (
        'Returned more than 1 {0} for instance_id {1}'.format(instances,
                                                              instance_id))
    return instances[0]


@task
def update_volumes_tags(filters=None):
    """Clone tags from instances to volumes.

    :param filters: apply optional filtering for the
                    :func:`django_fabfile.utils.get_all_instances`.
    """
    for region in regions():
        reservations = get_region_conn(region.name).get_all_instances(
            filters=filters)
        for res in reservations:
            inst = res.instances[0]
            for bdm in inst.block_device_mapping.keys():
                vol_id = inst.block_device_mapping[bdm].volume_id
                vol = inst.connection.get_all_volumes([vol_id])[0]
                add_tags(vol, inst.tags)


@contextmanager
def config_temp_ssh(conn):
    config_name = '{region}-temp-ssh-{now}'.format(
        region=conn.region.name, now=timestamp())
    key_pair = conn.create_key_pair(config_name)
    key_filename = key_pair.name + '.pem'
    key_pair.save('./')
    os.chmod(key_filename, 0600)
    try:
        yield os.path.realpath(key_filename)
    finally:
        key_pair.delete()
        os.remove(key_filename)


def new_security_group(region, name=None, description=None):
    """Create Security Groups with SSH access."""
    s_g = get_region_conn(region.name).create_security_group(
        name or 'Created on {0}'.format(timestamp()),
        description or 'Created for using with specific instance')
    s_g.authorize('tcp', 22, 22, '0.0.0.0/0')
    return s_g


@task
def cleanup_security_groups(delete=False):
    """
    Delete unused AWS Security Groups.

    :type delete: boolean
    :param delete: notify only by default.

    If security group with the same name is used at least in one region,
    it is treated as used.
    """
    groups = defaultdict(lambda: {})
    used_groups = set(['default',])
    regions = get_region_conn().get_all_regions()
    for reg in regions:
        for s_g in get_region_conn(reg.name).get_all_security_groups():
            groups[s_g.name][reg] = s_g
            if s_g.instances():     # Security Group is used by instance.
                used_groups.add(s_g.name)
            for rule in s_g.rules:
                for grant in rule.grants:
                    if grant.name and grant.owner_id == s_g.owner_id:
                        used_groups.add(s_g.name)   # SG is used by group.
    for grp in used_groups:
        del groups[grp]

    for grp in sorted(groups):
        if delete:
            for reg in groups[grp]:
                s_g = groups[grp][reg]
                logger.info('Deleting {0} in {1}'.format(s_g, reg))
                s_g.delete()
        else:
            msg = '"SecurityGroup:{grp}" should be removed from {regs}'
            logger.info(msg.format(grp=grp, regs=groups[grp].keys()))


def regroup_rules(security_group):
    grouped_rules = defaultdict(lambda: [])
    for rule in security_group.rules:
        ports = rule.ip_protocol, rule.from_port, rule.to_port
        for grant in rule.grants:
            grouped_rules[ports].append(grant)
    return grouped_rules


def sync_rules(src_grp, dst_grp):
    """
    Copy Security Group rules.

    Works across regions as well. The sole exception is granted groups,
    owned by another user - such groups can't be copied recursively.
    """

    def is_group_in(region, group_name):
        try:
            get_region_conn(region.name).get_all_security_groups([group_name])
        except EC2ResponseError:
            return False
        else:
            return True

    src_rules = regroup_rules(src_grp)
    # Assure granted group represented in destination region.
    src_grants = chain(*src_rules.values())
    for grant in dict((grant.name, grant) for grant in src_grants).values():
        if grant.name and grant.owner_id == src_grp.owner_id:
            if not is_group_in(dst_grp.region, grant.name):
                src_conn = get_region_conn(src_grp.region.name)
                grant_grp = src_conn.get_all_security_groups([grant.name])[0]
                dst_conn = get_region_conn(dst_grp.region.name)
                grant_copy = dst_conn.create_security_group(
                    grant_grp.name, grant_grp.description)
                sync_rules(grant_grp, grant_copy)
    dst_rules = regroup_rules(dst_grp)
    # Remove rules absent in src_grp.
    for ports in set(dst_rules.keys()) - set(src_rules.keys()):
        for grant in dst_rules[ports]:
            args = ports + ((None, grant) if grant.name else (grant, None))
            dst_grp.revoke(*args)
    # Add rules absent in dst_grp.
    for ports in set(src_rules.keys()) - set(dst_rules.keys()):
        for grant in src_rules[ports]:
            if grant.name and not is_group_in(dst_grp.region, grant.name):
                continue    # Absent other's granted group.
            args = ports + ((None, grant) if grant.name else (grant, None))
            dst_grp.authorize(*args)
    # Refresh `dst_rules` from updated `dst_grp`.
    dst_rules = regroup_rules(dst_grp)

    @contextmanager
    def patch_grouporcird():
        """XXX Patching `boto.ec2.securitygroup.GroupOrCIDR` cmp and hash."""
        from boto.ec2.securitygroup import GroupOrCIDR
        original_cmp = getattr(GroupOrCIDR, '__cmp__', None)
        GroupOrCIDR.__cmp__ = lambda self, other: cmp(str(self), str(other))
        original_hash = GroupOrCIDR.__hash__
        GroupOrCIDR.__hash__ = lambda self: hash(str(self))
        try:
            yield
        finally:
            if original_cmp:
                GroupOrCIDR.__cmp__ = original_cmp
            else:
                del GroupOrCIDR.__cmp__
            GroupOrCIDR.__hash__ = original_hash

    # Sync grants in common rules.
    with patch_grouporcird():
        for ports in src_rules:
            # Remove grants absent in src_grp rules.
            for grant in set(dst_rules[ports]) - set(src_rules[ports]):
                args = ports + ((None, grant) if grant.name else (grant, None))
                dst_grp.revoke(*args)
            # Add grants absent in dst_grp rules.
            for grant in set(src_rules[ports]) - set(dst_rules[ports]):
                if grant.name and not is_group_in(dst_grp.region, grant.name):
                    continue    # Absent other's granted group.
                args = ports + ((None, grant) if grant.name else (grant, None))
                dst_grp.authorize(*args)
