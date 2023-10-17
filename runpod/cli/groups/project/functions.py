'''
RunPod | CLI | Project | Functions
'''

import os
import shutil
import uuid
import tomlkit
from tomlkit import document, comment, table, nl

from runpod import create_pod, get_pod
from runpod.cli.utils.ssh_cmd import SSHConnection
from runpod import error as rp_error
from .helpers import get_project_pod
from ...utils.rp_sync import sync_directory

STARTER_TEMPLATES = os.path.join(os.path.dirname(__file__), 'starter_templates')

# -------------------------------- New Project ------------------------------- #
def create_new_project(project_name, runpod_volume_id, python_version,
                       model_type=None, model_name=None):
    """ Create a new project with the given name. """
    project_folder = os.path.join(os.getcwd(), project_name)
    if not os.path.exists(project_folder):
        os.makedirs(project_folder)

    if model_type is None:
        model_type = "default"

    template_dir = os.path.join(STARTER_TEMPLATES, model_type)

    for item in os.listdir(template_dir):
        source_item = os.path.join(template_dir, item)
        destination_item = os.path.join(project_folder, item)

        if os.path.isdir(source_item):
            shutil.copytree(source_item, destination_item)
        else:
            shutil.copy2(source_item, destination_item)

    # If there's a model_name, replace placeholders in handler.py
    if model_name:
        handler_path = os.path.join(project_folder, "handler.py")
        if os.path.exists(handler_path):
            with open(handler_path, 'r', encoding='utf-8') as file:
                handler_content = file.read()

            handler_content = handler_content.replace('<<MODEL_NAME>>', model_name)

            with open(handler_path, 'w', encoding='utf-8') as file:
                file.write(handler_content)

    toml_config = document()
    toml_config.add(comment('RunPod Project Configuration'))
    toml_config.add(nl())
    toml_config.add("tittle", project_name)

    project_table = table()
    project_table.add("uuid", str(uuid.uuid4())[:8])
    project_table.add("name", project_name)
    project_table.add("base_image", "runpod/base:0.0.1")
    project_table.add("gpu_types", ["NVIDIA RTX A4500"])
    project_table.add("gpu_count", 1)
    project_table.add("storage_id", runpod_volume_id)
    project_table.add("volume_mount_path", "/runpod-volume")
    project_table.add("ports", "8080/http, 22/tcp")
    project_table.add("container_disk_size_gb", 10)
    project_table.add("env_vars", {"RUNPOD_PROJECT_ID": "project_uuid"})
    toml_config.add("project", project_table)

    template_table = table()
    template_table.add("model_type", str(model_type))
    template_table.add("model_name", str(model_name))
    toml_config.add("template", template_table)

    runtime_table = table()
    runtime_table.add("python_version", python_version)
    runtime_table.add("requirements_path", os.path.join(project_folder, "requirements.txt"))
    toml_config.add("runtime", runtime_table)

    with open(os.path.join(project_folder, "runpod.toml"), 'w', encoding="UTF-8") as config_file:
        tomlkit.dump(toml_config, config_file)


# ------------------------------ Launch Project ------------------------------ #
def launch_project():
    '''
    Launch the project development environment from runpod.toml
    # SSH into the pod and create a project folder within the volume
    # Create a folder in the volume for the project that matches the project uuid
    # Create a folder in the project folder that matches the project name
    # Copy the project files into the project folder
    # crate a virtual environment using the python version specified in the project config
    # install the requirements.txt file
    '''
    project_file = os.path.join(os.getcwd(), 'runpod.toml')
    if not os.path.exists(project_file):
        raise FileNotFoundError("runpod.toml not found in the current directory.")

    with open(project_file, 'r', encoding="UTF-8") as config_file:
        config = tomlkit.load(config_file)

    for config_item in config['project']:
        print(f'{config_item}: {config["project"][config_item]}')

    # Check if the project pod already exists.
    if get_project_pod(config['project']['uuid']):
        raise ValueError('Project pod already launched. Run "runpod project start" to start.')

    print("Launching pod on RunPod...")
    environment_variables = {"RUNPOD_PROJECT_ID": config["project"]["uuid"]}
    for variable in config['project']['env_vars']:
        environment_variables[variable] = config['project']['env_vars'][variable]

    selected_gpu_types = config['project'].get('gpu_types',[])
    selected_gpu_types.append(config['project'].get('gpu_types', None))

    new_pod = None
    successful_gpu_type = None
    for gpu_type in selected_gpu_types:
        print(f"Trying to get a pod with {gpu_type}...")
        try:
            new_pod = create_pod(
                f'{config["project"]["Name"]}-dev ({config["project"]["uuid"]})',
                config['project']['BaseImage'],
                gpu_type,
                gpu_count=int(config['project']['GPUCount']),
                support_public_ip=True,
                ports=f'{config["project"]["Ports"]}',
                network_volume_id=f'{config["project"]["StorageID"]}',
                volume_mount_path=f'{config["project"]["VolumeMountPath"]}',
                container_disk_in_gb=int(config["project"]["ContainerDiskSizeGB"]),
                env={"RUNPOD_PROJECT_ID": config["project"]["uuid"]}
            )
            successful_gpu_type = gpu_type
            break
        except rp_error.QueryError:
            print(f"Couldn't obtain a {gpu_type}")
    if new_pod is None:
        print("Couldn't obtain any of the selected gpu types, try again later or use a different type")
        return
    print(f"Got a pod with {successful_gpu_type} ({new_pod['id']})")

    print("Waiting for pod to come online...")
    while new_pod.get('desiredStatus', None) != 'RUNNING' or new_pod.get('runtime', None) is None:
        new_pod = get_pod(new_pod['id'])

    print(f"Project {config['project']['name']} pod ({new_pod['id']}) created.")

    ssh_conn = SSHConnection(new_pod['id'])
    project_files = os.listdir(os.getcwd())

    project_path_uuid = f'{config["project"]["volume_mount_path"]}/{config["project"]["uuid"]}'
    project_path = f'{project_path_uuid}/{config["project"]["name"]}'

    print(f'Creating project folder: {project_path} on pod {new_pod["id"]}')
    ssh_conn.run_commands([f'mkdir -p {project_path}'])

    for file in project_files:
        if os.path.isdir(file):
            ssh_conn.put_directory(file, f'{project_path}/{file}')
        else:
            ssh_conn.put_file(file, f'{project_path}/{file}')

    venv_path = os.path.join(project_path_uuid, "venv")

    print(f'Creating virtual environment: {venv_path} on pod {new_pod["id"]}')
    commands = [
        f'python{config["runtime"]["python_version"]} -m venv {venv_path}',
        f'source {venv_path}/bin/activate &&' \
        f'cd {project_path} &&' \
        'pip install --upgrade pip &&'
        'pip install -r requirements.txt'
    ]

    ssh_conn.run_commands(commands)


# ------------------------------- Start Project ------------------------------ #
def start_project_api():
    '''
    python handler.py --rp_serve_api --rp_api_host="0.0.0.0" --rp_api_port=8080
    '''
    project_file = os.path.join(os.getcwd(), 'runpod.toml')
    if not os.path.exists(project_file):
        raise FileNotFoundError("runpod.toml not found in the current directory.")

    with open(project_file, 'r', encoding="UTF-8") as config_file:
        config = tomlkit.load(config_file)

    project_pod = get_project_pod(config['project']['uuid'])
    if project_pod is None:
        raise ValueError(f'Project pod not found for uuid: {config["project"]["uuid"]}')

    ssh_conn = SSHConnection(project_pod['id'])

    volume_mount_path = config["project"]["VolumeMountPath"]
    project_uuid = config["project"]["uuid"]
    project_name = config["project"]["Name"]
    remote_project_path = f'{volume_mount_path}/{project_uuid}/{project_name}'
    requirements_path = f"{remote_project_path}/{config['ENVIRONMENT']['RequirementsPath']}"

    # ssh_conn.rsync(os.path.join(os.getcwd(), ''), remote_project_path)
    sync_directory(ssh_conn, os.getcwd(), remote_project_path)

    launch_api_server = [f'''
        pkill inotify

        function cleanup {{
            echo "Cleaning up..."
            kill $last_pid 2>/dev/null
        }}

        trap cleanup EXIT

        if source {volume_mount_path}/{project_uuid}/venv/bin/activate; then
            echo "Activated virtual environment."
        else
            echo "Failed to activate virtual environment."
            exit 1
        fi

        if cd {volume_mount_path}/{project_uuid}/{project_name}; then
            echo "Changed to project directory."
        else
            echo "Failed to change directory."
            exit 1
        fi

        python handler.py --rp_serve_api --rp_api_host="0.0.0.0" --rp_api_port=8080 &
        last_pid=$!
        echo "Started API server with PID: $last_pid"

        while true; do
            if changed_file=$(inotifywait -r -e modify,create,delete --exclude '(__pycache__|\\.pyc$)' {remote_project_path} --format '%w%f'); then
                echo "Detected changes in: $changed_file"
            else
                echo "Failed to detect changes."
                exit 1
            fi

            if kill $last_pid; then
                echo "Killed API server with PID: $last_pid"
            else
                echo "Failed to kill server."
                exit 1
            fi

            if [[ $changed_file == {requirements_path} ]]; then
                pip install --upgrade pip && pip install -r {requirements_path}
            fi

            sleep 1 #Debounce

            python handler.py --rp_serve_api --rp_api_host="0.0.0.0" --rp_api_port=8080 &
            last_pid=$!
            echo "Restarted API server with PID: $last_pid"
        done
    ''']



    try:
        ssh_conn.run_commands(launch_api_server)
    finally:
        ssh_conn.close()
