from ConfigParser import ConfigParser as _ConfigParser

from fabric.api import env, sudo, settings
from fabric.state import output


config_file = 'fabfile.cfg'
config = _ConfigParser()
config.read(config_file)
master = config.get('RDBMS', 'master')
backup = config.get('RDBMS', 'backup')
username = config.get('odesk', 'username')
pcp_password = config.get('RDBMS', 'pcp_password')

env.update({'disable_known_hosts': True, 'user': username, 'warn_only': True})
output.update({'running': False})


def _return(master, backup, node_id):
    with settings(host_string=master):
        sudo('su postgres -c "pcp_attach_node 60 127.0.0.1 9898 postgres'
          ' {pcp_password} {node_id}"'.format(node_id=node_id,
                                                    pcp_password=pcp_password))
    with settings(host_string=backup):
        sudo('su postgres -c "pcp_attach_node 60 127.0.0.1 9898 postgres'
          ' {pcp_password} {node_id}"'.format(node_id=node_id,
                                                    pcp_password=pcp_password))


def _failover(new_primary_host, old_primary_host, failed_node_id,
                                                    master_node_id):
    trigger = '/var/log/pgpool/trigger/trigger_file1'
    with settings(host_string=new_primary_host):
        sudo('su postgres -c "touch {trigger}"'.format(trigger=trigger))
        sudo('su postgres -c "/usr/local/etc/dnsmadeeasy-update.sh'
                      ' {new_master}"'.format(new_master=new_primary_host))
        sudo('su postgres -c "pcp_detach_node 60 127.0.0.1 9898 postgres'
          ' {pcp_password} {node_id}"'.format(node_id=failed_node_id,
                                                    pcp_password=pcp_password))
        sudo('su postgres -c "pcp_attach_node 60 127.0.0.1 9898 postgres'
          ' {pcp_password} {node_id}"'.format(node_id=master_node_id,
                                                    pcp_password=pcp_password))
    with settings(host_string=old_primary_host):
        sudo('su postgres -c "pcp_detach_node 60 127.0.0.1 9898 postgres'
          ' {pcp_password} {node_id}"'.format(node_id=failed_node_id,
                                                    pcp_password=pcp_password))
        sudo('su postgres -c "pcp_attach_node 60 127.0.0.1 9898 postgres'
          ' {pcp_password} {node_id}"'.format(node_id=master_node_id,
                                                    pcp_password=pcp_password))


def _recover(primary, standby, recover_node_id):
    with settings(host_string=primary):
        out = sudo('/etc/init.d/postgresql status | grep -c Running')
        if out == '1':
            sudo('/etc/init.d/postgresql stop')
    with settings(host_string=standby):
        sudo('su postgres -c "pcp_recovery_node 60 127.0.0.1 9898 postgres'
          ' {pcp_password} {node_id}"'.format(node_id=recover_node_id,
                                                    pcp_password=pcp_password))
    with settings(host_string=primary):
        sudo('su postgres -c "pcp_attach_node 60 127.0.0.1 9898 postgres'
          ' {pcp_password} {node_id}"'.format(node_id=recover_node_id,
                                                    pcp_password=pcp_password))


def switch_to_backup():
    """
    If master server failed, run this to promote backup server
    from slave to master-role and detach failed node from pgpool
    """
    _failover(backup, master, 0, 1)


def recover_master():
    """
    Run this to start recovery of failing master node
    (run this strictly after switching_to_backup)
    """
    _recover(master, backup, 0)


def return_master():
    """
    Run this after temporary network issues in us-east-1
    It will reattach master node to pgpool server
    """
    _return(master, backup, 0)


def switch_to_master():
    """
    If backup server failed, run this to promote master server
    from slave to master-role and detach failed node from pgpool
    """
    _failover(master, backup, 1, 0)


def recover_backup():
    """
    Run this to start recovery of failing backup node
    (run this strictly after switching_to_master)
    """
    _recover(backup, master, 1)


def return_backup():
    """
    Run this after temporary network issues in eu-west-1
    It will reattach backup node to pgpool server
    """
    _return(master, backup, 1)
