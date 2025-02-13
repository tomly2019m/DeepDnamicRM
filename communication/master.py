import argparse
import json
import os
import socket
import sys
import time
import asyncio
from typing import Dict, Tuple
import paramiko
from sync import distribute_project

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from monitor.data_collector import *

parser = argparse.ArgumentParser()
parser.add_argument("--exp_time", type=int, default=30, help="experiment time")
parser.add_argument("--username", type=str, default="tomly", help="username for SSH connection")
parser.add_argument("--save", action="store_true", help="whether to save data")

args = parser.parse_args()

exp_time = args.exp_time
username = args.username
save     = args.save

gathered_list = []  # 用于存储每次循环处理后的 gathered 数据
replicas_list = []  # 用于存储每次循环的 replicas 数据

class SlaveConnection:
    def __init__(self, slave_host, slave_port):
        self.slave_host = slave_host
        self.slave_port = slave_port
        self.socket = None

    async def connect(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((self.slave_host, self.slave_port))
        print(f"Connected to slave at {self.slave_host}:{self.slave_port}")

    async def send_command(self, command) -> str:
        if self.socket:
            self.socket.sendall(command.encode())
            data = self.socket.recv(20480)
            # print(f'Received from {self.slave_host}:{self.slave_port}:', data.decode())
            return data.decode()

    def close(self):
        if self.socket:
            self.socket.close()
            print(f"Connection to {self.slave_host}:{self.slave_port} closed.")


async def start_experiment(slaves):
    global exp_time, gathered_list, replicas_list
    connections : Dict[Tuple[str, int], SlaveConnection] = {}
    tasks = []

    # 建立与每个slave的连接
    for slave_host, slave_port in slaves:
        connection = SlaveConnection(slave_host, slave_port)
        await connection.connect()
        connections[(slave_host, slave_port)] = connection
        tasks.append(asyncio.create_task(connection.send_command("init")))

    # 等待所有 slave 初始化完毕
    await asyncio.gather(*tasks)
    tasks.clear()

    try:
        while True:
            gathered = {
                'cpu':{},
                'memory':{},
                'io':{},
                'network':{}
            }
            # 遍历所有slave连接，发送collect命令采集数据
            tasks.clear()
            for connection in connections.values():
                tasks.append(asyncio.create_task(connection.send_command("collect")))
            results = await asyncio.gather(*tasks)
            # s = time.time()
            for result in results:
                data_dict = json.loads(result)
                gathered['cpu'] = concat_data(gathered['cpu'], data_dict['cpu'])
                gathered['memory'] = concat_data(gathered['memory'], data_dict['memory'])
                gathered['io'] = concat_data(gathered['io'], data_dict['io'])
                gathered['network'] = concat_data(gathered['network'], data_dict['network'])
            replicas = np.array([len(cpu_list) for cpu_list in gathered['cpu'].values()]).flatten()
            # print(replicas)
            for k, v in gathered['cpu'].items():
                gathered['cpu'][k] = [item / 1e6 for item in v]
            gathered['cpu'] = process_data(gathered['cpu'])
            gathered['memory'] = process_data(gathered['memory'])
            gathered['io'] = process_data(gathered['io'])
            gathered['network'] = process_data(gathered['network'])
            gathered = transform_data(gathered)
            # print(time.time() - s)
            gathered_list.append(gathered)  # 将处理后的 gathered 数据存储到列表中
            replicas_list.append(replicas)  # 将处理后的 replicas 数据存储到列表中
            time.sleep(1)
            exp_time -= 1
            
            # 实验结束
            if exp_time == 0:
                break
    finally:
        for connection in connections.values():
            connection.close()


# 配置好slave，在slave上启动监听
def setup_slave():
    # 从配置文件中读取主机名和端口
    comm_config = ''
    with open("./comm.json", 'r') as f:
        comm_config = json.load(f)
    hosts = comm_config["slaves"] 
    port = comm_config["port"]

    # 在每个slave节点上启动监听服务
    for host in hosts:
        # 通过SSH连接到slave节点
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(host, username=username)
            # 先切换到目标目录在slave节点上启动监听程序

            # 清理旧的进程
            command = f"sudo kill -9 $(sudo lsof -t -i :{port})"
            stdin, stdout, stderr = ssh.exec_command(command)

            command = (
                'cd ~/DeepDynamicRM/communication && '
                'nohup ~/miniconda3/envs/DDRM/bin/python3 '
                f'slave.py --port {port} > /dev/null 2>&1 &'
            )
            
            stdin, stdout, stderr = ssh.exec_command(command)
            
            print(f'在 {host} 上启动监听服务,端口:{port}')
            
        except Exception as e:
            print(f'连接到 {host} 失败: {str(e)}')
        finally:
            ssh.close()

def save_data(gathered_list, replicas_list):
    """保存实验数据到本地文件"""
    # 创建数据目录(如果不存在)
    if not os.path.exists('./data'):
        os.makedirs('./data')

    # 保存gathered数据
    gathered_path = f'./data/gathered.npy'
    np.save(gathered_path, gathered_list)
    print(f"已保存gathered数据到: {gathered_path}")
    
    # 保存replicas数据 
    replicas_path = f'./data/replicas.npy'
    np.save(replicas_path, replicas_list)
    print(f"已保存replicas数据到: {replicas_path}")

async def main():
    global gathered_list, replicas_list
    # 从配置文件中读取主机名和端口，然后创建连接
    comm_config = ''
    with open("./comm.json", 'r') as f:
        comm_config = json.load(f)
    hosts = comm_config["slaves"]
    port = comm_config["port"]
    slaves = [(host, port) for host in hosts]
    
    distribute_project(username=username)
    setup_slave()
    # 等待slave监听进程启动完成
    time.sleep(5)
    await start_experiment(slaves)
    if save:
        save_data(gathered_list, replicas_list)

def test_setup_slave():
    # setup_slave()
    print("🔧 开始测试slave节点配置...")
    
    # 从配置文件中读取主机名和端口
    with open("./comm.json", 'r') as f:
        comm_config = json.load(f)
    hosts = comm_config["slaves"]
    port = comm_config["port"]

    # 测试每个slave节点的连通性
    for host in hosts:
        try:
            # 创建socket连接测试
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)  # 设置超时时间为5秒
                result = s.connect_ex((host, port))
                
                if result == 0:
                    print(f"✅ {host}:{port} 连接成功")
                else:
                    print(f"❌ {host}:{port} 连接失败")
                    
        except Exception as e:
            print(f"⚠️ 测试 {host} 时发生错误: {str(e)}")

    print("🔍 slave节点配置测试完成")

if __name__ == "__main__":
    asyncio.run(main())

