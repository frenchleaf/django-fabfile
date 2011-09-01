"""Check README or `django_fabfile.utils.Config` docstring for setup
instructions."""

import logging
import os
import re
from datetime import timedelta, datetime
from contextlib import nested
from itertools import groupby
from json import dumps

from boto.exception import EC2ResponseError
from fabric.api import env, local, put, settings, sudo, task
from fabric.contrib.files import append

from django_fabfile.instances import (attach_snapshot, create_temp_inst,
                                      get_avail_dev, get_vol_dev, mount_volume)
from django_fabfile.utils import (
    Config, StateNotChangedError, add_tags, config_temp_ssh,
    get_descr_attr, get_inst_by_id, get_region_conn,
    get_snap_time, get_snap_vol, get_snap_device,
    wait_for, wait_for_sudo)


config = Config()
username = config.get('DEFAULT', 'USERNAME')
env.update({'user': username, 'disable_known_hosts': True})

logger = logging.getLogger(__name__)


_now = lambda: datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S')


def create_snapshot(vol, description='', tags=None, synchronously=True):
    """Return new snapshot for the volume.

    vol
        volume to snapshot;
    synchronously
        wait for successful completion;
    description
        description for snapshot. Will be compiled from instnace
        parameters by default;
    tags
        tags to be added to snapshot. Will be cloned from volume by
        default."""
    if vol.attach_data:
        inst = get_inst_by_id(vol.region, vol.attach_data.instance_id)
    else:
        inst = None
    if not description and inst:
        description = dumps({
            'Volume': vol.id,
            'Region': vol.region.name,
            'Device': vol.attach_data.device,
            'Instance': inst.id,
            'Type': inst.instance_type,
            'Arch': inst.architecture,
            'Root_dev_name': inst.root_device_name,
            'Time': _now(),
            })

    def initiate_snapshot():
        snapshot = vol.create_snapshot(description)
        if tags:
            add_tags(snapshot, tags)
        else:
            if inst:
                add_tags(snapshot, inst.tags)
            add_tags(snapshot, vol.tags)
        logger.info('{0} initiated from {1}'.format(snapshot, vol))
        return snapshot

    if synchronously:
        timeout = config.getint('DEFAULT', 'MINUTES_FOR_SNAP')
        while True:     # Iterate unless success and delete failed snapshots.
            snapshot = initiate_snapshot()
            try:
                wait_for(snapshot, '100%', limit=timeout * 60)
                assert snapshot.status == 'completed', (
                    'completed with wrong status {0}'.format(snapshot.status))
            except (StateNotChangedError, AssertionError) as err:
                logger.error(str(err) + ' - deleting')
                snapshot.delete()
            else:
                break
    else:
        snapshot = initiate_snapshot()
    return snapshot


@task
def backup_instance(region_name, instance_id=None, instance=None,
                    synchronously=False):
    """Return list of created snapshots for specified instance.

    region_name
        instance location;
    instance, instance_id
        either `instance_id` or `instance` argument should be specified;
    synchronously
        wait for completion. False by default."""
    assert bool(instance_id) ^ bool(instance), ('Either instance_id or '
        'instance should be specified')
    conn = get_region_conn(region_name)
    if instance_id:
        instance = get_inst_by_id(conn.region, instance_id)
    snapshots = []
    for dev in instance.block_device_mapping:
        vol_id = instance.block_device_mapping[dev].volume_id
        vol = conn.get_all_volumes([vol_id])[0]
        snapshots.append(create_snapshot(vol, synchronously=synchronously))
    return snapshots


@task
def backup_instances_by_tag(region_name=None, tag_name=None, tag_value=None,
                            synchronously=False):
    """Creates backup for all instances with given tag in region.

    region_name
        will be applied across all regions by default;
    tag_name, tag_value
        will be fetched from config by default, may be configured
        per region;
    synchronously
        will be accomplished without checking results. False by default.
        NOTE: when ``create_ami`` task compiles AMI from several
        snapshots it restricts snapshot start_time difference with 10
        minutes interval at most. Snapshot completion may take much more
        time and due to this only asynchronously generated snapshots
        will be assembled assurely."""
    if region_name:
        regions = [get_region_conn(region_name).region]
    else:
        regions = get_region_conn().get_all_regions()
    for reg in regions:
        tag_name = tag_name or config.get(reg.name, 'TAG_NAME')
        tag_value = tag_value or config.get(reg.name, 'TAG_VALUE')
        conn = get_region_conn(reg.name)
        filters = {'resource-type': 'instance', 'key': tag_name,
                   'tag-value': tag_value}
        for tag in conn.get_all_tags(filters=filters):
            backup_instance(reg.name, instance_id=tag.res_id,
                            synchronously=synchronously)


def _trim_snapshots(conn, dry_run=False):

    """Delete snapshots back in time in logarithmic manner.

    dry_run
        just print snapshot to be deleted."""
    hourly_backups = config.getint('purge_backups', 'HOURLY_BACKUPS')
    daily_backups = config.getint('purge_backups', 'DAILY_BACKUPS')
    weekly_backups = config.getint('purge_backups', 'WEEKLY_BACKUPS')
    monthly_backups = config.getint('purge_backups', 'MONTHLY_BACKUPS')
    quarterly_backups = config.getint('purge_backups', 'QUARTERLY_BACKUPS')
    yearly_backups = config.getint('purge_backups', 'YEARLY_BACKUPS')

    # work with UTC time, which is what the snapshot start time is reported in
    now = datetime.utcnow()
    last_hour = datetime(now.year, now.month, now.day, now.hour)
    last_midnight = datetime(now.year, now.month, now.day)
    last_sunday = datetime(now.year, now.month,
          now.day) - timedelta(days=(now.weekday() + 1) % 7)
    last_month = datetime(now.year, now.month - 1, now.day)
    last_year = datetime(now.year - 1, now.month, now.day)
    other_years = datetime(now.year - 2, now.month, now.day)
    start_of_month = datetime(now.year, now.month, 1)

    target_backup_times = []
    # there are no snapshots older than 1/1/2000
    oldest_snapshot_date = datetime(2000, 1, 1)

    for hour in range(0, hourly_backups):
        target_backup_times.append(last_hour - timedelta(hours=hour))

    for day in range(0, daily_backups):
        target_backup_times.append(last_midnight - timedelta(days=day))

    for week in range(0, weekly_backups):
        target_backup_times.append(last_sunday - timedelta(weeks=week))

    for month in range(0, monthly_backups):
        target_backup_times.append(last_month - timedelta(weeks=month * 4))

    for quart in range(0, quarterly_backups):
        target_backup_times.append(last_year - timedelta(weeks=quart * 16))

    for year in range(0, yearly_backups):
        target_backup_times.append(other_years - timedelta(days=year * 365))

    one_day = timedelta(days=1)
    while start_of_month > oldest_snapshot_date:
        # append the start of the month to the list of snapshot dates to save:
        target_backup_times.append(start_of_month)
        # there's no timedelta setting for one month, so instead:
        # decrement the day by one,
        #so we go to the final day of the previous month...
        start_of_month -= one_day
        # ... and then go to the first day of that previous month:
        start_of_month = datetime(start_of_month.year,
                               start_of_month.month, 1)

    temp = []

    for t in target_backup_times:
        if temp.__contains__(t) == False:
            temp.append(t)

    target_backup_times = temp
    target_backup_times.reverse()  # make the oldest date first

    # get all the snapshots, sort them by date and time,
    #and organize them into one array for each volume:
    all_snapshots = conn.get_all_snapshots(owner='self')
    # oldest first
    all_snapshots.sort(cmp=lambda x, y: cmp(x.start_time, y.start_time))

    snaps_for_each_volume = {}
    for snap in all_snapshots:
        # the snapshot name and the volume name are the same.
        # The snapshot name is set from the volume
        # name at the time the snapshot is taken
        volume_name = get_snap_vol(snap)

        if volume_name:
            # only examine snapshots that have a volume name
            snaps_for_volume = snaps_for_each_volume.get(volume_name)

            if not snaps_for_volume:
                snaps_for_volume = []
                snaps_for_each_volume[volume_name] = snaps_for_volume
            snaps_for_volume.append(snap)

    # Do a running comparison of snapshot dates to desired time periods,
    # keeping the oldest snapshot in each
    # time period and deleting the rest:
    for volume_name in snaps_for_each_volume:
        snaps = snaps_for_each_volume[volume_name]
        snaps = snaps[:-1]
        # never delete the newest snapshot, so remove it from consideration

        time_period_num = 0
        snap_found_for_this_time_period = False
        for snap in snaps:
            check_this_snap = True

            while (check_this_snap and
                   time_period_num < target_backup_times.__len__()):

                if get_snap_time(snap) < target_backup_times[time_period_num]:
                    # the snap date is before the cutoff date.
                    # Figure out if it's the first snap in this
                    # date range and act accordingly
                    #(since both date the date ranges and the snapshots
                    # are sorted chronologically, we know this
                    #snapshot isn't in an earlier date range):
                    if snap_found_for_this_time_period:
                        if not snap.tags.get('preserve_snapshot'):
                            if dry_run:
                                logger.info('Dry-trimmed {0} {1} from {2}'
                                    .format(snap, snap.description,
                                    snap.start_time))
                            else:
                                # as long as the snapshot wasn't marked with
                                # the 'preserve_snapshot' tag, delete it:
                                try:
                                    conn.delete_snapshot(snap.id)
                                except EC2ResponseError as err:
                                    logger.exception(str(err))
                                else:
                                    logger.info('Trimmed {0} {1} from {2}'
                                        .format(snap, snap.description,
                                        snap.start_time))
                       # go on and look at the next snapshot,
                       # leaving the time period alone
                    else:
                        # this was the first snapshot found for this time
                        # period. Leave it alone and look at the next snapshot:
                        snap_found_for_this_time_period = True
                    check_this_snap = False
                else:
                    # the snap is after the cutoff date.
                    # Check it against the next cutoff date
                    time_period_num += 1
                    snap_found_for_this_time_period = False


@task
def delete_broken_snapshots():
    """Delete snapshots with status 'error'."""
    for region in get_region_conn().get_all_regions():
        conn = region.connect()
        filters = {'status': 'error'}
        snaps = conn.get_all_snapshots(owner='self', filters=filters)
        for snp in snaps:
            logger.info('Deleting broken {0}'.format(snp))
            snp.delete()


@task
def trim_snapshots(region_name=None, dry_run=False):
    """Delete old snapshots logarithmically back in time.

    region_name
        by default process all regions;
    dry_run
        boolean, only print info about old snapshots to be deleted."""
    delete_broken_snapshots()
    if region_name:
        regions = [get_region_conn(region_name)]
    else:
        regions = get_region_conn().get_all_regions()
    for reg in regions:
        logger.info('Processing {0}'.format(reg))
        _trim_snapshots(reg, dry_run=dry_run)


@task
def rsync_mountpoints(src_inst, src_vol, src_mnt, dst_inst, dst_vol, dst_mnt,
                      encr):
    """Run `rsync` against mountpoints."""
    src_key_filename = config.get(src_inst.region.name, 'KEY_FILENAME')
    dst_key_filename = config.get(dst_inst.region.name, 'KEY_FILENAME')
    with config_temp_ssh(dst_inst.connection) as key_file:
        with settings(host_string=dst_inst.public_dns_name,
                      key_filename=dst_key_filename):
            wait_for_sudo('cp /root/.ssh/authorized_keys '
                          '/root/.ssh/authorized_keys.bak')
            pub_key = local('ssh-keygen -y -f {0}'.format(key_file), True)
            append('/root/.ssh/authorized_keys', pub_key, use_sudo=True)
            if encr:
                sudo('screen -d -m sh -c "nc -l 60000 | gzip -dfc | '
                     'sudo dd of={0} bs=16M"'
                     .format(get_vol_dev(dst_vol)), pty=False)  # dirty magick
                dst_ip = sudo(
                    'curl http://169.254.169.254/latest/meta-data/public-ipv4')

        with settings(host_string=src_inst.public_dns_name,
                      key_filename=src_key_filename):
            put(key_file, '.ssh/', mirror_local_mode=True)
            dst_key_filename = os.path.split(key_file)[1]
            if encr:
                sudo('(dd if={0} bs=16M | gzip -cf --fast | nc -v {1} 60000)'
                     .format(get_vol_dev(src_vol), dst_ip))
            else:
                cmd = (
                'rsync -e "ssh -i .ssh/{key_file} -o StrictHostKeyChecking=no"'
                ' -aHAXz --delete --exclude /root/.bash_history '
                '--exclude /home/*/.bash_history --exclude /etc/ssh/moduli '
                '--exclude /etc/ssh/ssh_host_* '
                '--exclude /etc/udev/rules.d/*persistent-net.rules '
                '--exclude /var/lib/ec2/* --exclude=/mnt/* --exclude=/proc/* '
                '--exclude=/tmp/* {src_mnt}/ root@{rhost}:{dst_mnt}')
                wait_for_sudo(cmd.format(
                    rhost=dst_inst.public_dns_name, dst_mnt=dst_mnt,
                    key_file=dst_key_filename, src_mnt=src_mnt))
                label = sudo('e2label {0}'.format(get_vol_dev(src_vol)))
                with settings(host_string=dst_inst.public_dns_name,
                      key_filename=dst_key_filename):
                    sudo('e2label {0} {1}'.format(get_vol_dev(dst_vol), label))
                    wait_for_sudo('mv /root/.ssh/authorized_keys.bak '
                                  '/root/.ssh/authorized_keys')


def update_snap(src_vol, src_mnt, dst_vol, dst_mnt, encr, delete_old=False):

    """Update destination region from `src_vol`.

    Create new snapshot with same description and tags. Delete previous
    snapshot (if exists) of the same volume in destination region if
    ``delete_old`` is True."""

    src_inst = get_inst_by_id(src_vol.region, src_vol.attach_data.instance_id)
    dst_inst = get_inst_by_id(dst_vol.region, dst_vol.attach_data.instance_id)
    rsync_mountpoints(src_inst, src_vol, src_mnt, dst_inst, dst_vol, dst_mnt,
                     encr)
    if dst_vol.snapshot_id:
        old_snap = dst_vol.connection.get_all_snapshots(
            [dst_vol.snapshot_id])[0]
    else:
        old_snap = None
    src_snap = src_vol.connection.get_all_snapshots([src_vol.snapshot_id])[0]
    create_snapshot(dst_vol, description=src_snap.description,
                                    tags=src_snap.tags, synchronously=False)
    if old_snap and delete_old:
        logger.info('Deleting previous {0} in {1}'.format(old_snap,
                                                          dst_vol.region))
        old_snap.delete()


def create_empty_snapshot(region, size):
    """Format new filesystem."""
    with create_temp_inst(region) as inst:
        vol = region.connect().create_volume(size, inst.placement)
        earmarking_tag = config.get(region.name, 'TAG_NAME')
        vol.add_tag(earmarking_tag, 'temporary')
        vol.attach(inst.id, get_avail_dev(inst))
        mount_volume(vol, mkfs=True)
        snap = vol.create_snapshot()
        snap.add_tag(earmarking_tag, 'temporary')
        vol.detach(True)
        wait_for(vol, 'available')
        vol.delete()
        return snap


@task
def rsync_snapshot(src_region_name, snapshot_id, dst_region_name,
                   src_inst=None, dst_inst=None):

    """Duplicate the snapshot into dst_region.

    src_region_name, dst_region_name
        Amazon region names. Allowed to be contracted, e.g.
        `ap-southeast-1` will be recognized in `ap-south` or even
        `ap-s`;
    snapshot_id
        snapshot to duplicate;
    src_inst, dst_inst
        will be used instead of creating new for temporary.
    You'll need to open port 60000 for encrypted instances replication
    """
    src_conn = get_region_conn(src_region_name)
    src_snap = src_conn.get_all_snapshots([snapshot_id])[0]
    dst_conn = get_region_conn(dst_region_name)
    _src_device = get_snap_device(src_snap)
    _src_dev = re.match(r'^/dev/sda$', _src_device)  # check for encryption
    if _src_dev:
        encr = True
        logger.info('Found traces of encryption')
    else:
        encr = None

    info = 'Going to transmit {snap.volume_size} GiB {snap} {snap.description}'
    if src_snap.tags.get('Name'):
        info += ' of {name}'
    info += ' from {snap.region} to {dst}'
    logger.info(info.format(snap=src_snap, dst=dst_conn.region,
                            name=src_snap.tags.get('Name')))

    dst_snaps = dst_conn.get_all_snapshots(owner='self')
    dst_snaps = [snp for snp in dst_snaps if not snp.status == 'error']
    src_vol = get_snap_vol(src_snap)
    vol_snaps = [snp for snp in dst_snaps if get_snap_vol(snp) == src_vol]

    if vol_snaps:
        dst_snap = sorted(vol_snaps, key=get_snap_time)[-1]
        if get_snap_time(dst_snap) >= get_snap_time(src_snap):
            kwargs = dict(src=src_snap, dst=dst_snap, dst_reg=dst_conn.region)
            logger.info('Stepping over {src} - it\'s not newer than {dst} '
                        '{dst.description} in {dst_reg}'.format(**kwargs))
            return
    else:
        dst_snap = create_empty_snapshot(dst_conn.region, src_snap.volume_size)

    with nested(attach_snapshot(src_snap, inst=src_inst, encr=encr),
                attach_snapshot(dst_snap, inst=dst_inst, encr=encr)) as (
                (src_vol, src_mnt), (dst_vol, dst_mnt)):
        update_snap(src_vol, src_mnt, dst_vol, dst_mnt, encr,
                    delete_old=not vol_snaps)  # Delete only empty snapshots.


@task
def rsync_region(src_region_name, dst_region_name, tag_name=None,
                 tag_value=None, native_only=True):
    """Duplicates latest snapshots with given tag into dst_region.

    src_region_name, dst_region_name
        every latest volume snapshot from src_region will be `rsync`ed
        to the dst_region;
    tag_name, tag_value
        snapshots will be filtered by tag. Tag will be fetched from
        config by default, may be configured per region;
    native_only
        sync only snapshots, created in the src_region_name. True by
        default."""
    src_conn = get_region_conn(src_region_name)
    dst_conn = get_region_conn(dst_region_name)
    tag_name = tag_name or config.get(src_conn.region.name, 'TAG_NAME')
    tag_value = tag_value or config.get(src_conn.region.name, 'TAG_VALUE')
    filters = {'tag-key': tag_name, 'tag-value': tag_value}
    snaps = src_conn.get_all_snapshots(owner='self', filters=filters)
    snaps = [snp for snp in snaps if not snp.status == 'error']
    _is_described = lambda snap: get_snap_vol(snap) and get_snap_time(snap)
    snaps = [snp for snp in snaps if _is_described(snp)]
    if native_only:

        def is_native(snap, region):
            return get_descr_attr(snap, 'Region') == region.name
        snaps = [snp for snp in snaps if is_native(snp, src_conn.region)]

    with nested(create_temp_inst(src_conn.region),
                create_temp_inst(dst_conn.region)) as (src_inst, dst_inst):
        snaps = sorted(snaps, key=get_snap_vol)    # Prepare for grouping.
        for vol, vol_snaps in groupby(snaps, get_snap_vol):
            latest_snap = sorted(vol_snaps, key=get_snap_time)[-1]
            for inst in src_inst, dst_inst:
                logger.debug('Rebooting {0} in {0.region} '
                             'to refresh attachments'.format(inst))
                inst.reboot()
            args = (src_region_name, latest_snap.id, dst_region_name, src_inst,
                    dst_inst)
            try:
                rsync_snapshot(*args)
            except:
                logger.exception('rsync of {1} from {0} to {2} failed'.format(
                    *args))
