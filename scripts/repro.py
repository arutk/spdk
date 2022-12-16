#!/usr/bin/python3

import subprocess
import time
import sys
import json
import os

if len(sys.argv) < 3:
	print(f"usage: {sys.argv[0]} <spdk_directory> <test device BDF>")
	exit(1)

spdk_dir=sys.argv[1]
disk_bdf=sys.argv[2]

rpc_cmd = f"{spdk_dir}/scripts/rpc.py"
tgt_cmd = f"{spdk_dir}/build/bin/spdk_tgt"
dd_cmd = f"{spdk_dir}/build/bin/spdk_dd"

def run(cmd, env=None):
	print(cmd)
	p = subprocess.run(cmd, universal_newlines=True, shell=True, stdout=subprocess.PIPE,
			stderr=subprocess.PIPE, env=env)
	return p.returncode, p.stdout, p.stderr

def run_check_rc(cmd, env=None):
	rc, stdo, stde = run(cmd, env=env)
	assert rc == 0
	return rc, stdo, stde

def start(cmd, env=None):
	print(cmd)
	return subprocess.Popen(cmd, universal_newlines=True, shell=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def run_with_timeout(cmd, timeout, env=None):
	p = start(cmd, env=env)
	time.sleep(timeout)
	if not p.poll():
		p.kill()
		p.wait()

	stdo, stde = p.communicate()
	return p.returncode, stdo, stde

def wait_for_start(p):
	ret = -1
	while ret != 0:
		if p.poll():
			print("ERROR: failed to start application")
			stdo, stde = p.communicate()
			print(stdo)
			print(stde)
			exit(1)
		ret, stdo, stde = run_with_timeout(f"{rpc_cmd} rpc_get_methods", 1)
	return True

def get_lvols():
	rc, stdo, stde = run(f"{rpc_cmd}  bdev_get_bdevs")
	assert rc == 0
	bdevs = json.loads(stdo)
	return [dev for dev in bdevs if 'lvol' in dev['driver_specific']]

## make sure there are no residual applications running
if run("pidof spdk_tgt")[0] == 0:
	print("spdk_tgt is running, please close it")
	exit(1)
if run("pidof spdk_dd")[0] == 0:
	print("spdk_dd is running, please close it")
	exit(1)

cluster_size = 1024 * 1024 * 1024

def iteration(cfg, dd_offset = 0):
	#first dd run
	dd_args = f"{dd_cmd} -m 0x2 -c <(echo '{cfg}') --if /dev/urandom --ob lvs0/lvol0 --bs 4096 --count 1958912 --seek {dd_offset}"
	p = start(dd_args)
	wait_for_start(p)

	# wait for some I/O
	time.sleep(5)

	# kill -9 spdk_dd
	p.kill()
	stdo, stde = p.communicate()

	# load config in spdk tgt
	p = start(f"{tgt_cmd} -m 0x2 -c <(echo '{cfg}')")
	wait_for_start(p)

	# check if lvol loaded
	lvols = get_lvols()
	if len(lvols) == 0:
		print("FAIL - no lvol loaded. leaving spdk_tgt running.")
		exit(1)

	p.terminate()
	p.communicate()

# start spdk tgt
p = start(f"{tgt_cmd} -m 0x2")
wait_for_start(p)

# attach test disk
run_check_rc(f"{rpc_cmd} bdev_nvme_attach_controller --name nvme0 --trtype PCIe --traddr {disk_bdf}")

# cleanup
run(f"{rpc_cmd} bdev_lvol_delete lvs0/lvol0")
run(f"{rpc_cmd} bdev_lvol_delete_lvstore -l lvs0")

# create lvstore
run_check_rc(f"{rpc_cmd} bdev_lvol_create_lvstore -c {cluster_size} --clear-method none nvme0n1 lvs0")

# get lvstore size
rc, stdo, stde = run(f"{rpc_cmd} bdev_lvol_get_lvstores")
lvstores = json.loads(stdo)
assert len(lvstores) == 1
num_clusters = lvstores[0]['total_data_clusters']

# create lvol using all available lvstore space
run_check_rc(f"{rpc_cmd} bdev_lvol_create -l lvs0 -t lvol0 {num_clusters * cluster_size // 1024 // 1024}")

# save config
rc, stdo, stde = run(f"{rpc_cmd} save_subsystem_config -n bdev")
assert rc == 0
cfg = '{"subsystems": [' + stdo + '] }'

# shut down spdk_tgt
p.terminate()
p.communicate()

iteration(cfg, dd_offset=0)
iteration(cfg, dd_offset=924672)
print("PASS")
