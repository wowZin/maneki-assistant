from scripts.l2_daemon_client import daemon_alive
print('DAEMON ALIVE' if daemon_alive() else 'DAEMON DEAD')
