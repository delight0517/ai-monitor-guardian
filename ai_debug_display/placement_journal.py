"""Atomic local placement metadata; no titles, content, credentials or pixels."""
import json
import os
from pathlib import Path
import uuid
from .reconnect_recovery import SavedPlacement,WindowIdentity


def _object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate_json_key')
        result[key]=value
    return result


def _nonfinite(value):
    raise ValueError('nonfinite_json_value')


def _keys(obj,names):
    if type(obj) is not dict or set(obj)!=set(names):raise ValueError('invalid_journal_object_fields')


class PlacementJournal:
    SCHEMA_VERSION=1

    def __init__(self,path):
        self.path=Path(path)

    @staticmethod
    def _records_to_dto(records):
        if not isinstance(records,(list,tuple)) or len(records)>1000:raise ValueError('invalid_placement_record_list')
        rows=[];seen=set()
        for record in records:
            if not isinstance(record,SavedPlacement) or record.identity.hwnd in seen:raise ValueError('invalid_or_duplicate_placement')
            seen.add(record.identity.hwnd)
            i=record.identity
            rows.append({'identity':{'hwnd':i.hwnd,'pid':i.pid,'process_started':i.process_started,'executable_path':i.executable_path},
                         'physical_output_id':record.physical_output_id,'rect':list(record.rect)})
        return {'schema_version':1,'records':rows}

    def save(self,records):
        dto=self._records_to_dto(records)
        # Encode before touching the existing good file.
        payload=json.dumps(dto,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if self.path.is_symlink():raise ValueError('journal_symlink_not_allowed')
        temporary=self.path.with_name(self.path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            with open(temporary,'xb') as stream:
                stream.write(payload);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,self.path)
        finally:
            if temporary.exists():temporary.unlink()

    def load(self):
        if self.path.is_symlink():raise ValueError('journal_symlink_not_allowed')
        if self.path.stat().st_size>1024*1024:raise ValueError('journal_too_large')
        dto=json.loads(self.path.read_text(encoding='utf-8'),object_pairs_hook=_object,parse_constant=_nonfinite)
        _keys(dto,('schema_version','records'))
        if type(dto['schema_version']) is not int or dto['schema_version']!=1:raise ValueError('unsupported_journal_schema')
        if type(dto['records']) is not list or len(dto['records'])>1000:raise ValueError('invalid_placement_record_list')
        records=[];seen=set()
        for row in dto['records']:
            _keys(row,('identity','physical_output_id','rect'))
            identity=row['identity'];_keys(identity,('hwnd','pid','process_started','executable_path'))
            if type(row['rect']) is not list:raise ValueError('journal_rectangle_must_be_array')
            record=SavedPlacement(WindowIdentity(**identity),row['physical_output_id'],row['rect'])
            if record.identity.hwnd in seen:raise ValueError('duplicate_saved_window')
            seen.add(record.identity.hwnd);records.append(record)
        return tuple(records)
