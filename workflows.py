"""Durable, expiring conversational state; no personal names in callbacks."""
from collections.abc import MutableMapping
from database import db

class WorkflowMap(MutableMapping):
    def __init__(self,kind):self.kind=kind
    def __getitem__(self,key):
        value=db.get_workflow(int(key),self.kind)
        if value is None:raise KeyError(key)
        return value
    def __setitem__(self,key,value):db.set_workflow(int(key),self.kind,value)
    def __delitem__(self,key):db.set_workflow(int(key),self.kind,None)
    def __iter__(self):return iter(db.workflow_users(self.kind))
    def __len__(self):return len(db.workflow_users(self.kind))
