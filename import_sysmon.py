"""Map exported Windows Event XML, not binary EVTX, to normalized replay input.
Only Sysmon process create (1) and network connection (3) are supported.
Missing fields remain missing. There is no inferred privilege transition.
"""
import argparse,json,xml.etree.ElementTree as ET
from pathlib import Path
from engine import normalize

def convert(xml):
 if not isinstance(xml,str) or len(xml.encode('utf-8'))>16_000_000:raise ValueError('XML must be a string under 16 MB')
 if '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():raise ValueError('DTD and entity declarations are not accepted')
 try:root=ET.fromstring(xml)
 except ET.ParseError as e:raise ValueError('Invalid XML: '+str(e)) from e
 rows=[]
 nodes=[root] if root.tag.split('}')[-1]=='Event' else root.findall('.//{*}Event')
 for event in nodes:
  system=event.find('{*}System')
  if system is None:raise ValueError('Missing event System element')
  provider=system.find('{*}Provider');kind=system.findtext('{*}EventID')
  if provider is None or provider.get('Name')!='Microsoft-Windows-Sysmon' or kind not in ['1','3']:
   raise ValueError('Only Sysmon events 1 and 3 are supported; split other events first')
  time=system.find('{*}TimeCreated');d={x.get('Name'):x.text for x in event.findall('{*}EventData/{*}Data')}
  row={'event_id':system.findtext('{*}EventRecordID'),'source_id':system.findtext('{*}Computer'),
       'timestamp':time.get('SystemTime') if time is not None else None,
       'event_type':'sysmon_process_create' if kind=='1' else 'sysmon_network_connection',
       'action':'process_create' if kind=='1' else 'network_connection','raw_payload':ET.tostring(event,encoding='unicode')}
  for source,target in [('ProcessGuid','process_id'),('ParentProcessGuid','parent_id'),('User','principal'),('LogonGuid','session_id')]:
   if d.get(source):row[target]=d[source]
  if kind=='3':
   initiated=(d.get('Initiated') or '').lower()
   if initiated in ['true','false']:
    row['direction']='outbound' if initiated=='true' else 'inbound'
    remote=d.get('DestinationIp' if initiated=='true' else 'SourceIp')
    if remote:row['remote_ip']=remote
  rows.append(row)
 return normalize(rows)

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('input_xml');p.add_argument('output_json');a=p.parse_args()
 events=convert(Path(a.input_xml).read_text(encoding='utf-8-sig'));Path(a.output_json).write_text(json.dumps(events,indent=2));print('Converted',len(events),'events. Select contracts appropriate to the available fields before ingest.')
if __name__=='__main__':main()
