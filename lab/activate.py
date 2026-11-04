#!/usr/bin/env python3
"""Activate ONOS providers via REST and disable conflicting reactive fwd app."""
import os
import requests

base = os.getenv('ONOS_URL','http://127.0.0.1:8181').rstrip('/')+'/onos/v1/'
session=requests.Session()
session.auth=(os.getenv('ONOS_USER','onos'),os.getenv('ONOS_PASSWORD','rocks'))

def request(method,path):
    response=session.request(method,base+path,timeout=(3,10))
    response.raise_for_status()
    print(method,path,response.status_code)


if __name__ == '__main__':
    for app in ('drivers', 'openflow', 'lldpprovider', 'hostprovider'):
        request('POST', f'applications/org.onosproject.{app}/active')
    try:
        request('DELETE', 'applications/org.onosproject.fwd/active')
    except requests.HTTPError as exc:
        if exc.response.status_code != 404:
            raise
