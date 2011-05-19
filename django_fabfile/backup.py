# -*- coding: utf-8 -*-
'''
Use configuration file ~/.boto for storing your credentials as described
at http://code.google.com/p/boto/wiki/BotoConfig#Credentials

All other options will be saved in ./fabfile.cfg file.
'''

from pprint import PrettyPrinter as _PrettyPrinter
from pydoc import pager as _pager
from re import compile as _compile
from time import sleep as _sleep
from warnings import warn as _warn
from boto.ec2 import (connect_to_region as _connect_to_region,
                      regions as _regions)
from boto.exception import BotoServerError as _BotoServerError
from ConfigParser import ConfigParser as _ConfigParser
import os, sys
from datetime import timedelta, datetime
from fabric.api import env, prompt, sudo

config_file = 'fabfile.cfg'
config = _ConfigParser()
config.readfp(open(config_file))
region = config.get('main', 'region')
instance_id = config.get('main', 'instance_id')

hourly_backups = config.getint('purge_backups', 'hourly_backups')
daily_backups = config.getint('purge_backups', 'daily_backups')
weekly_backups = config.getint('purge_backups', 'weekly_backups')
monthly_backups = config.getint('purge_backups', 'monthly_backups')
quarterly_backups = config.getint('purge_backups', 'quarterly_backups')
yearly_backups = config.getint('purge_backups', 'yearly_backups')
dry_run = config.getboolean('purge_backups', 'dry_run')

username = config.get('mount_backups', 'username')
device = config.get('mount_backups', 'device')
mountpoint = config.get('mount_backups', 'mountpoint')
ubuntu_aws_account = config.get('mount_backups', 'ubuntu_aws_account')
architecture = config.get('mount_backups', 'architecture')
root_device_type = config.get('mount_backups', 'root_device_type')
ami_ptrn = config.get('mount_backups', 'ami_ptrn')
ami_ptrn_with_version = config.get('mount_backups', 'ami_ptrn_with_version')
ami_ptrn_with_relase_date = config.get('mount_backups', 'ami_ptrn_with_relase_date')
ami_regexp = config.get('mount_backups', 'ami_regexp')
key_pair = config.get(region, 'key_pair')
key_filename = config.get(region, 'key_filename')

conn = _connect_to_region(region)

def _get_instance_by_id(region, instance_id):
    res = conn.get_all_instances([instance_id,])
    assert len(res) == 1, (
        'Returned more than 1 {0} for instance_id {1}'.format(res, instance_id))
    instances = res[0].instances
    assert len(instances) == 1, (
        'Returned more than 1 {0} for instance_id {1}'.format(instances,
                                                              instance_id))
    return instances[0]


def create_snapshot(region, instance_id=None, instance=None, dev='/dev/sda1'):
    """Return newly created snapshot of specified instance device.

    Either `instance_id` or `instance` should be specified."""
    assert bool(instance_id) ^ bool(instance), ('Either instance_id or '
        'instance should be specified')
    if instance_id:
        instance = _get_instance_by_id(region, instance_id)
    vol_id = instance.block_device_mapping[dev].volume_id
    description = 'Created from {0} of {1}'.format(dev, instance.id)
    if instance.tags.get('Name'):
        description += ' named as {0}'.format(instance.tags['Name'])
    snapshot = conn.create_snapshot(vol_id, description)
    for tag in instance.tags:   # Clone intance tags to the snapshot.
        snapshot.add_tag(tag, instance.tags[tag])
    print 'Waiting for the {snap} to be completed...'.format(snap=snapshot)
    while snapshot.status != 'completed':
        print 'still {snap.status}...'.format(snap=snapshot)
        _sleep(5)
        snapshot.update()
    print 'done.'
    return snapshot


def backup_instance(region=region, instance_id=instance_id):
    """Return list of created snapshots for specified instance."""
    instance = _get_instance_by_id(region, instance_id)
    snapshots = []  # NOTE Fabric doesn't supports generators.
    for dev in instance.block_device_mapping:
        snapshots.append(create_snapshot(region, instance=instance, dev=dev))
    return snapshots


def upload_snapshot_to_s3(region, snapshot_id, bucket):
    """`snapshot_id` will be used as filename in the `bucket`."""
    pass


def fetch_snapshot_from_s3(region, snapshot_id, bucket):
    """`snapshot_id` name will be searched withit the the `bucket`."""
    pass

def trim_snapshots(conn = conn, hourly_backups = hourly_backups, daily_backups = daily_backups, weekly_backups = weekly_backups, monthly_backups = monthly_backups, quarterly_backups = quarterly_backups, yearly_backups = yearly_backups, dry_run = dry_run):
    now = datetime.utcnow() # work with UTC time, which is what the snapshot start time is reported in
    last_hour = datetime(now.year, now.month, now.day, now.hour)
    last_midnight = datetime(now.year, now.month, now.day)
    last_sunday = datetime(now.year, now.month, now.day) - timedelta(days = (now.weekday() + 1) % 7)
    last_month = datetime(now.year, now.month -1, now.day)
    last_year = datetime(now.year-1, now.month, now.day)
    other_years = datetime(now.year-2, now.month, now.day)
    start_of_month = datetime(now.year, now.month, 1)

    target_backup_times = []

    oldest_snapshot_date = datetime(2000, 1, 1) # there are no snapshots older than 1/1/2002

    for hour in range(0, hourly_backups):
        target_backup_times.append(last_hour - timedelta(hours = hour))

    for day in range(0, daily_backups):
        target_backup_times.append(last_midnight - timedelta(days = day))

    for week in range(0, weekly_backups):
        target_backup_times.append(last_sunday - timedelta(weeks = week))

    for month in range(0, monthly_backups):
        target_backup_times.append(last_month- timedelta(weeks= month*4))

    for quart in range(0, quarterly_backups):
        target_backup_times.append(last_year- timedelta(weeks= quart*16))

    for year in range(0, yearly_backups):
        target_backup_times.append(other_years- timedelta(days = year*365))

    #print target_backup_times

    one_day = timedelta(days = 1)
    while start_of_month > oldest_snapshot_date:
        # append the start of the month to the list of snapshot dates to save:
        target_backup_times.append(start_of_month)
        # there's no timedelta setting for one month, so instead:
        # decrement the day by one, so we go to the final day of the previous month...
        start_of_month -= one_day
        # ... and then go to the first day of that previous month:
        start_of_month = datetime(start_of_month.year, start_of_month.month, 1)

    temp = []

    for t in target_backup_times:
        if temp.__contains__(t) == False:
            temp.append(t)

    target_backup_times = temp
    target_backup_times.reverse() # make the oldest date first

    # get all the snapshots, sort them by date and time, and organize them into one array for each volume:
    all_snapshots = conn.get_all_snapshots(owner = 'self')
    all_snapshots.sort(cmp = lambda x, y: cmp(x.start_time, y.start_time)) # oldest first
    snaps_for_each_volume = {}
    for snap in all_snapshots:
        # the snapshot name and the volume name are the same. The snapshot name is set from the volume
        # name at the time the snapshot is taken
        volume_name = snap.volume_id
        #print volume_name
        if volume_name:
            # only examine snapshots that have a volume name
            snaps_for_volume = snaps_for_each_volume.get(volume_name)
            #print snaps_for_volume
            if not snaps_for_volume:
                snaps_for_volume = []
                snaps_for_each_volume[volume_name] = snaps_for_volume
            snaps_for_volume.append(snap)
            #print snaps_for_volume

    # Do a running comparison of snapshot dates to desired time periods, keeping the oldest snapshot in each
    # time period and deleting the rest:
    for volume_name in snaps_for_each_volume:
        snaps = snaps_for_each_volume[volume_name]
        snaps = snaps[:-1] # never delete the newest snapshot, so remove it from consideration
        #print snaps
        time_period_number = 0
        snap_found_for_this_time_period = False
        for snap in snaps:
            check_this_snap = True
            #print snap
            while check_this_snap and time_period_number < target_backup_times.__len__():
                snap_date = datetime.strptime(snap.start_time, '%Y-%m-%dT%H:%M:%S.000Z')
                #print snap_date
                if snap_date < target_backup_times[time_period_number]:
                    # the snap date is before the cutoff date. Figure out if it's the first snap in this
                    # date range and act accordingly (since both date the date ranges and the snapshots
                    # are sorted chronologically, we know this snapshot isn't in an earlier date range):
                    if snap_found_for_this_time_period == True:
                        if not snap.tags.get('preserve_snapshot'):
                            if dry_run == True:
                                print('Dry-trimmed snapshot %s (%s)' % (snap, snap.start_time))
                            else:
                                # as long as the snapshot wasn't marked with the 'preserve_snapshot' tag, delete it:
                                conn.delete_snapshot(snap.id)
                                print('Trimmed snapshot %s (%s)' % (snap, snap.start_time))
                       # go on and look at the next snapshot, leaving the time period alone
                    else:
                       # this was the first snapshot found for this time period. Leave it alone and look at the
                       # next snapshot:
                       snap_found_for_this_time_period = True
                    check_this_snap = False
                else:
                   # the snap is after the cutoff date. Check it against the next cutoff date
                   #print 1
                   time_period_number += 1
                   snap_found_for_this_time_period = False

def _wait_for(obj, attrs, state, update_attr='update', max_sleep=30):
    """Wait for attribute to go into state.

    attrs
        list of nested attribute names;
    update_attr
        will be called to refresh state."""
    print 'Waiting for the {0} to be {1}...'.format(obj, state)
    def get_nested_attr(obj, attrs):
        attr = obj
        for attr_name in attrs:
            attr = getattr(attr, attr_name)
        return attr
    sleep_for = 3
    while get_nested_attr(obj, attrs) != state:
        print 'still {0}...'.format(get_nested_attr(obj, attrs))
        sleep_for += 2
        _sleep(min(sleep_for, max_sleep))
        getattr(obj, update_attr)()
    print 'done.'

def create_instance(region_name='us-east-1', zone_name=None):

    """Create AWS EC2 instance.

    Return created instance.

    zone
        string-formatted name. By default will be used latest zone."""

    info = ('Please enter keypair name in the {0} region for person who will '
            'access the instance').format(region_name)

    conn = _connect_to_region(region_name)

    filters={'owner_id': ubuntu_aws_account, 'architecture': architecture,
             'name': ami_ptrn, 'image_type': 'machine',
             'root_device_type': root_device_type}
    images = conn.get_all_images(filters=filters)

    # Filtering by latest version.
    ptrn = _compile(ami_regexp)
    versions = set([ptrn.search(img.name).group('version') for img in images])
    def complement(year_month):
        return '0' + year_month if len(year_month) == 4 else year_month
    latest_version = sorted(set(filter(complement, versions)))[-1]  # XXX Y3K.
    name_with_version = ami_ptrn_with_version.format(version=latest_version)
    filters.update({'name': name_with_version})
    images = conn.get_all_images(filters=filters)
    # Filtering by latest release date.
    dates = set([ptrn.search(img.name).group('released_at') for img in images])
    latest_date = sorted(set(dates))[-1]
    name_with_version_and_release = ami_ptrn_with_relase_date.format(
        version=latest_version, released_at=latest_date)
    filters.update({'name': name_with_version_and_release})
    image = conn.get_all_images(filters=filters)[0]
    zone = zone_name or conn.get_all_zones()[-1].name
    print 'Launching new instance in {zone} from {image}'.format(image=image,
                                                                 zone=zone)
    reservation = image.run(key_name=key_pair, instance_type='t1.micro',
                            placement=zone)
    print '{res.instances[0]} created in {zone}.'.format(res=reservation,
                                                         zone=zone)

    assert len(reservation.instances) == 1, 'More than 1 instances created'

    return reservation.instances[0]


def _prompt_to_select(choices, query='Select from', paging=False):

    """Prompt to select an option from provided choices.

    choices: list or dict. If dict, then choice will be made among keys.
    paging: render long list with pagination.

    Return solely possible value instantly without prompting."""

    keys = list(choices)
    while keys.count(None):
        keys.pop(choices.index(None))    # Remove empty values.
    assert len(keys), 'No choices provided'

    if len(keys) == 1: return keys[0]

    picked = None
    while not picked in keys:
        if paging:
            pp = _PrettyPrinter()
            _pager(query + '\n' + pp.pformat(choices))
            text = 'Enter your choice or press Return to view options again'
        else:
            text = '{query} {choices}'.format(query=query, choices=choices)
        picked = prompt(text)
    return picked


def _get_all_instances(region=None, id_only=False):
    if not region:
        _warn('There is no guarantee of instance id uniqueness across regions')
    reg_names = [region] if region else (reg.name for reg in _regions())
    connections = (_connect_to_region(reg) for reg in reg_names)
    for con in connections:
        for res in con.get_all_instances():
            for inst in res.instances:
                yield inst.id if id_only else inst

def _get_all_snapshots(region=None, id_only=False):
    if not region:
        _warn('There is no guarantee of snapshot id uniqueness across regions')
    reg_names = [region] if region else (reg.name for reg in _regions())
    connections = (_connect_to_region(reg) for reg in reg_names)
    for con in connections:
        for snap in con.get_all_snapshots(owner='self'):
            yield snap.id if id_only else snap


def _select_snapshot():

    region_name = region

    snap_id = prompt('Please enter snapshot ID if it\'s known (press Return '
                     'otherwise)')
    if snap_id:
        if snap_id in _get_all_snapshots(region_name, id_only=True):
            return region_name, snap_id
        else:
            print 'No snapshot with provided ID found'

    #instances_list = list(_get_all_instances(region_name))
    #instances = dict((inst.id, {'Name': inst.tags.get('Name'),
                                #'State': inst.state,
                                #'Launched': inst.launch_time,
                                #'Key pair': inst.key_name,
                                #'Type': inst.instance_type,
                                #'IP Address': inst.ip_address,
                                #'DNS Name': inst.public_dns_name}
                     #) for inst in instances_list)
    #instance_id = _prompt_to_select(instances, 'Select instance ID from',
                                    #paging=True)

    all_instances = _get_all_instances(region_name)
    inst = [inst for inst in all_instances if inst.id == instance_id][0]
    volumes = dict((dev.volume_id, {'Status': dev.status,
                                    'Attached': dev.attach_time,
                                    'Size': dev.size,
                                    'Snapshot ID': dev.snapshot_id}
                   ) for dev in inst.block_device_mapping.values())
    volume_id = _prompt_to_select(volumes, 'Select volume ID from', paging=True)

    all_snaps = _get_all_snapshots(region_name)
    snaps_list = (snap for snap in all_snaps if snap.volume_id == volume_id)
    snaps = dict((snap.id, {'Volume': snap.volume_id,
                            'Date': snap.start_time,
                            'Description': snap.description}
                 ) for snap in snaps_list)
    return region_name, _prompt_to_select(snaps, 'Select snapshot ID from',
                                          paging=True)


def mount_snapshot(region=None, snap_id=None):

    """Mount snapshot to temporary created instance."""

    if not region or not snap_id:
        region, snap_id = _select_snapshot()
    conn = _connect_to_region(region)
    snap = conn.get_all_snapshots(snapshot_ids=[snap_id,])[0]

    def _mount_snapshot_in_zone(snap_id, zone):
        volume = inst = None
        try:
            volume = conn.create_volume(
                snapshot=snap.id, size=snap.volume_size, zone=zone)
            print 'New {vol} created from {snap} in {zone}.'.format(
                vol=volume, snap=snap, zone=zone)

            try:
                inst = create_instance(zone.region.name, zone.name)
                _wait_for(inst, ['state',], 'running')

                attach = volume.attach(inst.id, device)
                volume.update()
                _wait_for(volume, ['attach_data', 'status'], 'attached')
                print 'Volume is attached to {inst} as {dev}.'.format(
                    inst=inst, dev=device)

                info = ('Please enter private key location of your keypair '
                        'in {region}').format(region=zone.region)
                env.update({
                    'host_string': inst.public_dns_name,
                    'key_filename': key_filename,
                    'load_known_hosts': False,
                    'user': username,
                })
                while True:
                    try:
                        sudo('mkdir {mnt}'.format(mnt=mountpoint))
                        break
                    except:
                        print 'sshd still launching, waiting to try again...'
                        _sleep(5)
                info = ('\nYou may now SSH into the {inst} server, using:'
                        '\n ssh -i {key} {user}@{inst.public_dns_name}')
                try:
                    sudo('mount {dev} {mnt}'.format(dev=device, mnt=mountpoint))
                except:
                    info += ('\nand mount {device}. NOTE: device name may be '
                             'modified by system.')
                else:
                    info += ('\nand browse mounted at {mountpoint} backup '
                             'volume {device}.')
                print info.format(inst=inst, device=device, key=key_filename,
                                  user=username, mountpoint=mountpoint)

                info = ('\nEnter FINISHED if you are finished looking at the '
                        'backup and would like to cleanup: ')
                while raw_input(info).strip() != 'FINISHED':
                    pass

            # Cleanup processing: terminate temporary server.
            finally:
                if inst:
                    print 'Deleting the {0}...'.format(inst)
                    inst.terminate()
                    print 'done.'

        # Cleanup processing: delete detached backup volume.
        finally:
            if volume:
                _wait_for(volume, ['status',], 'available')

                print 'Deleting the backup {vol}...'.format(vol=volume)
                delete = volume.delete()
                print 'done.'

    for zone in conn.get_all_zones():
        try:
            _mount_snapshot_in_zone(snap_id, zone)
        except _BotoServerError, err:
            print '{0} in {1}'.format(err, zone)
            continue
        else:
            break