import gzip
import re
import requests
from argparse import ArgumentParser
from google.protobuf.json_format import MessageToJson, Parse, ParseError
from pathlib import Path
from subprocess import run

FORKS = {
    'mihon': 'mihonapp/mihon',
    'sy': 'jobobby04/TachiyomiSY',
    'j2k': 'Jays2Kings/tachiyomiJ2K',
}

PROTONUMBER_RE = r'(?:^\s*(?!\/\/\s*)@ProtoNumber\((?P<number>\d+)\)\s*|data class \w+\(|^)va[rl]\s+(?P<name>\w+):\s+(?:(?:(?:List|Set)<(?P<list>\w+)>)|(?P<type>\w+))(?P<optional>\?|(:?\s+=))?'
CLASS_RE = r'^(?:data )?class (?P<name>\w+)\((?P<defs>(?:[^)(]+|\((?:[^)(]+|\([^)(]*\))*\))*)\)'
DATA_TYPES = {
    'String': 'string',
    'Int': 'int32',
    'Long': 'int64',
    'Boolean': 'bool',
    'Float': 'float',
    # TODO: better handling of these,
    # they should be auto discovered
    # 'UpdateStrategy': 'int32',
    'PreferenceValue': 'bytes',
}

argp = ArgumentParser()
argp.add_argument(
    '--input',
    '-i',
    metavar='<backup_file.tachibk | backup_file.proto.gz | decoded_backup.json>',
    help='File extension defines whether to decode a backup file to JSON or encode it back',
    type=Path,
)
argp.add_argument(
    '--output',
    '-o',
    default='output.json',
    metavar='<output.json | encoded_backup.tachibk>',
    help='When encoding, TACHIBK or PROTO.GZ will additionally recompress the backup file',
    type=Path,
)
argp.add_argument(
    '--fork',
    default=list(FORKS.keys())[0],
    choices=FORKS.keys(),
    metavar=f'<{" | ".join(FORKS.keys())}>',
    help='Fork for the backup schema. Default: mihon',
)
args = argp.parse_args()


def fetch_schema(fork: str) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    git = requests.get(
        f'https://api.github.com/repos/{fork}/contents/app/src/main/java/eu/kanade/tachiyomi/data/backup/models'
    ).json()
    for entry in git:
        if entry.get('type') == 'file':
            files.append((entry.get('name'), entry.get('download_url')))
        elif entry.get('type') == 'dir':
            for sub_entry in requests.get(entry.get('url')).json():
                if sub_entry.get('type') == 'file':
                    files.append(
                        (sub_entry.get('name'), sub_entry.get('download_url'))
                    )
    return files


def parse_model(model: str) -> list[str]:
    data = requests.get(model).text
    message: list[str] = []
    for name in re.finditer(CLASS_RE, data, re.MULTILINE):
        message.append('message {name} {{'.format(name=name.group('name')))
        for field in re.finditer(
            PROTONUMBER_RE, name.group('defs'), re.MULTILINE
        ):
            message.append(
                '  {repeated} {type} {name} = {number};'.format(
                    repeated='repeated'
                    if field.group('list')
                    else 'optional'
                    if field.group('optional')
                    else 'required',
                    type=DATA_TYPES.get(
                        field.group('type'),
                        DATA_TYPES.get(
                            field.group('list'),
                            field.group('list') or field.group('type'),
                        ),
                    ),
                    name=field.group('name'),
                    number=field.group('number') or 1
                    if not name.group('name').startswith('Broken')
                    else int(field.group('number')) + 1,
                )
            )
        message.append('}\n')
    return message


try:
    from schema_pb2 import Backup
except (ModuleNotFoundError, NameError):
    print('No protobuf schema found...')
    # Hard-coded exceptions to make parsing easier
    schema = '''syntax = "proto2";

enum UpdateStrategy {
  ALWAYS_UPDATE = 0;
  ONLY_FETCH_ONCE = 1;
}

message PreferenceValue {
  required string type = 1;
  required bytes value = 2;
}

'''.splitlines()
    print(f'... Fetching from {args.fork.upper()}')
    for i in fetch_schema(FORKS[args.fork]):
        print(f'... Parsing {i[0]}')
        schema.append(f'// {i[0]}')
        schema.extend(parse_model(i[1]))
    print('\n'.join(schema), file=open('schema.proto', 'wt'))
    print('Generating Python sources...')
    try:
        run(['protoc', '--python_out=.', '--pyi_out=.', 'schema.proto'])
    except FileNotFoundError:
        print(
            'ERROR! Protoc not found.',
            'Download at https://github.com/protocolbuffers/protobuf/releases/latest',
        )
        exit(1)
    try:
        from schema_pb2 import Backup
    except (ModuleNotFoundError, NameError):
        print('ERROR! Still unable to find the protobuf schema. Aborting.')
        exit(1)


def read_backup(input: str) -> (str | bytes):
    if input.endswith('.tachibk') or input.endswith('.proto.gz'):
        with gzip.open(input, 'rb') as zip:
            backup_data = zip.read()
            with open('extracted_tachibk', 'wb') as file:
                file.write(backup_data)
    else:
        try:
            with open('extracted_tachibk', 'rb') as file:
                backup_data = file.read()
        except OSError:
            print('ERROR! No Backup to process.')
            argp.print_help()
            exit(1)
    return backup_data


def parse_backup(backup_data) -> Backup:
    message = Backup()
    message.ParseFromString(backup_data)
    return message


def write_json(message: Backup) -> None:
    with open(args.output, 'wt') as file:
        file.write(MessageToJson(message, ensure_ascii=False))
    print(f'Backup decoded to "{args.output}"')


def parse_json(input: str) -> bytes:
    try:
        with open(input, 'b') as file:
            json_string = file.read()
    except OSError:
        print('ERROR! Could not read the JSON file.')
        exit(1)
    try:
        return Parse(json_string, Backup()).SerializeToString()
    except ParseError:
        print('The input JSON file is invalid.')
        exit(1)


def write_backup(message: bytes) -> None:
    compression = True
    output = (
        'encoded_backup.tachibk'
        if str(args.output) == 'output.json'
        else str(args.output)
    )
    if output.endswith('.proto.gz') or output.endswith('.tachibk'):
        with gzip.open(output, 'wb') as zip:
            zip.write(message)
    else:
        with open(output, 'wb') as file:
            file.write(message)
        compression = False
    print(
        f'{"C" if compression else "Unc"}ompressed backup written to {output}'
    )


if __name__ == '__main__':
    input = str(args.input)
    if input.endswith('.json'):
        write_backup(parse_json(input))
    else:
        write_json(parse_backup(read_backup(input)))
