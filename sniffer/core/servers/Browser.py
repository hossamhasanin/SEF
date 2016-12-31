#!/usr/bin/env python
# This file is part of Responder
# Original work by Laurent Gaffie - Trustwave Holdings
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import socket
import struct
import threading
import core.responder.settings as settings

from core.responder.packets import SMBHeader, SMBNegoData, SMBSessionData, SMBTreeConnectData, RAPNetServerEnum3Data, SMBTransRAPData
from SocketServer import BaseRequestHandler, ThreadingMixIn, UDPServer
from core.responder.utils import *

def start():
	try:
		server = ThreadingUDPServer(('', 138), Browser1)
		t = threading.Thread(name='Browser', target=server.serve_forever)
		t.setDaemon(True)
		t.start()
	except Exception as e:
		print "Error starting Browser server on port 138: {}".format(e)

class ThreadingUDPServer(ThreadingMixIn, UDPServer):
	def server_bind(self):
		if OsInterfaceIsSupported():
			try:
				self.socket.setsockopt(socket.SOL_SOCKET, 25, settings.Config.Bind_To+'\0')
			except:
				pass
		UDPServer.server_bind(self)

def WorkstationFingerPrint(data):
	Role = {
		"\x04\x00"    :"Windows 95",
		"\x04\x10"    :"Windows 98",
		"\x04\x90"    :"Windows ME",
		"\x05\x00"    :"Windows 2000",
		"\x05\x00"    :"Windows XP",
		"\x05\x02"    :"Windows 2003",
		"\x06\x00"    :"Windows Vista/Server 2008",
		"\x06\x01"    :"Windows 7/Server 2008R2",
	}

	return Role[data] if data in Role else "Unknown"

def RequestType(data):
	Type = {
		"\x01": 'Host Announcement',
		"\x02": 'Request Announcement',
		"\x08": 'Browser Election',
		"\x09": 'Get Backup List Request',
		"\x0a": 'Get Backup List Response',
		"\x0b": 'Become Backup Browser',
		"\x0c": 'Domain/Workgroup Announcement',
		"\x0d": 'Master Announcement',
		"\x0e": 'Reset Browser State Announcement',
		"\x0f": 'Local Master Announcement',
	}

	return Type[data] if data in Type else "Unknown"

def PrintServerName(data, entries):
	if entries > 0:

		entrieslen = 26*entries
		chunks, chunk_size = len(data[:entrieslen]), entrieslen/entries
		ServerName = [data[i:i+chunk_size] for i in range(0, chunks, chunk_size)]

		l = []
		for x in ServerName:
			FP   = WorkstationFingerPrint(x[16:18])
			Name = x[:16].replace('\x00', '')

			if FP:
				l.append(Name + ' (%s)' % FP)
			else:
				l.append(Name)

		return l

	return None

def ParsePacket(Payload):
	PayloadOffset = struct.unpack('<H',Payload[51:53])[0]
	StatusCode = Payload[PayloadOffset-4:PayloadOffset-2]

	if StatusCode == "\x00\x00":
		EntriesNum = struct.unpack('<H',Payload[PayloadOffset:PayloadOffset+2])[0]
		return PrintServerName(Payload[PayloadOffset+4:], EntriesNum)

	else:
		return None

def RAPThisDomain(Client,Domain):		
	PDC = RapFinger(Client,Domain,"\x00\x00\x00\x80")
	if PDC is not None:
		settings.Config.ResponderLogger.info("[LANMAN] Detected Domains: %s" % ', '.join(PDC))
	
	SQL = RapFinger(Client,Domain,"\x04\x00\x00\x00")
	if SQL is not None:
		settings.Config.ResponderLogger.info("[LANMAN] Detected SQL Servers on domain %s: %s" % (Domain, ', '.join(SQL)))

	WKST = RapFinger(Client,Domain,"\xff\xff\xff\xff")
	if WKST is not None:
		settings.Config.ResponderLogger.info("[LANMAN] Detected Workstations/Servers on domain %s: %s" % (Domain, ', '.join(WKST)))

def RapFinger(Host, Domain, Type):
	try:
		s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		s.connect((Host,445))
		s.settimeout(0.3)

		Header = SMBHeader(cmd="\x72",mid="\x01\x00")
		Body = SMBNegoData()
		Body.calculate()

		Packet = str(Header)+str(Body)
		Buffer = struct.pack(">i", len(''.join(Packet))) + Packet

		s.send(Buffer)
		data = s.recv(1024)

		# Session Setup AndX Request, Anonymous.
		if data[8:10] == "\x72\x00":
			Header = SMBHeader(cmd="\x73",mid="\x02\x00")
			Body = SMBSessionData()
			Body.calculate()

			Packet = str(Header)+str(Body)
			Buffer = struct.pack(">i", len(''.join(Packet))) + Packet

			s.send(Buffer)
			data = s.recv(1024)

			# Tree Connect IPC$.
			if data[8:10] == "\x73\x00":
				Header = SMBHeader(cmd="\x75",flag1="\x08", flag2="\x01\x00",uid=data[32:34],mid="\x03\x00")
				Body = SMBTreeConnectData(Path="\\\\"+Host+"\\IPC$")
				Body.calculate()

				Packet = str(Header)+str(Body)
				Buffer = struct.pack(">i", len(''.join(Packet))) + Packet

				s.send(Buffer)
				data = s.recv(1024)

				# Rap ServerEnum.
				if data[8:10] == "\x75\x00":
					Header = SMBHeader(cmd="\x25",flag1="\x08", flag2="\x01\xc8",uid=data[32:34],tid=data[28:30],pid=data[30:32],mid="\x04\x00")
					Body = SMBTransRAPData(Data=RAPNetServerEnum3Data(ServerType=Type,DetailLevel="\x01\x00",TargetDomain=Domain))
					Body.calculate()

					Packet = str(Header)+str(Body)
					Buffer = struct.pack(">i", len(''.join(Packet))) + Packet

					s.send(Buffer)
					data = s.recv(64736)

					# Rap ServerEnum, Get answer and return what we're looking for.
					if data[8:10] == "\x25\x00":
						s.close()
						return ParsePacket(data)
	except:
		pass

def BecomeBackup(data,Client):
	try:
		DataOffset    = struct.unpack('<H',data[139:141])[0]
		BrowserPacket = data[82+DataOffset:]
		ReqType       = RequestType(BrowserPacket[0])

		if ReqType == "Become Backup Browser":
			ServerName = BrowserPacket[1:]
			Domain     = Decode_Name(data[49:81])
			Name       = Decode_Name(data[15:47])
			Role       = NBT_NS_Role(data[45:48])

			if settings.Config.AnalyzeMode:
				settings.Config.AnalyzeLogger.warning("[Analyze mode: Browser] Datagram Request from IP: %s hostname: %s via the: %s wants to become a Local Master Browser Backup on this domain: %s."%(Client, Name,Role,Domain))
				print RAPThisDomain(Client, Domain)

	except:
		pass

def ParseDatagramNBTNames(data,Client):
	try:
		Domain = Decode_Name(data[49:81])
		Name   = Decode_Name(data[15:47])
		Role1  = NBT_NS_Role(data[45:48])
		Role2  = NBT_NS_Role(data[79:82])

	
		if Role2 == "Domain Controller" or Role2 == "Browser Election" or Role2 == "Local Master Browser" and settings.Config.AnalyzeMode:
			settings.Config.AnalyzeLogger.warning('[Analyze mode: Browser] Datagram Request from IP: %s hostname: %s via the: %s to: %s. Service: %s' % (Client, Name, Role1, Domain, Role2))
			print RAPThisDomain(Client, Domain)
	except:
		pass

class Browser1(BaseRequestHandler):

	def handle(self):
		try:
			request, socket = self.request

			if settings.Config.AnalyzeMode:
				ParseDatagramNBTNames(request,self.client_address[0])
				BecomeBackup(request,self.client_address[0])

			BecomeBackup(request,self.client_address[0])

		except Exception:
			pass