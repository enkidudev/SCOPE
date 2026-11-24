import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from import_sysmon import convert
from engine import Store

def xml(kind,data):
 fields=''.join('<Data Name="'+k+'">'+v+'</Data>' for k,v in data.items())
 return '<Events xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><Event><System><Provider Name="Microsoft-Windows-Sysmon"/><EventID>'+kind+'</EventID><EventRecordID>7</EventRecordID><Computer>LAB</Computer><TimeCreated SystemTime="2026-11-09T00:00:00Z"/></System><EventData>'+fields+'</EventData></Event></Events>'
class ImportTests(unittest.TestCase):
 def test_process_identity_parent(self):
  e=convert(xml('1',{'ProcessGuid':'p2','ParentProcessGuid':'p1','LogonGuid':'s1','User':'LAB\\u'}))
  s=Store(':memory:');r=s.ingest(e,['EC-01','EC-02','EC-05']);self.assertEqual(s.investigate(r['id'],'EC-02')['total_matches'],1)
  self.assertNotIn('privilege_before',e[0]);s.db.close()
 def test_network_inbound(self):
  e=convert(xml('3',{'ProcessGuid':'p2','Initiated':'false','SourceIp':'203.0.113.5','DestinationIp':'10.0.0.1'}))
  self.assertEqual(e[0]['remote_ip'],'203.0.113.5');self.assertEqual(e[0]['direction'],'inbound')
 def test_unsupported_event(self):
  with self.assertRaises(ValueError):convert(xml('10',{}))
