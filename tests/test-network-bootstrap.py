#!/usr/bin/python3
"""First-boot persistence, interface choice, and administrator configuration guards."""
import importlib.machinery
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
network = types.ModuleType('network_bootstrap')
importlib.machinery.SourceFileLoader('network_bootstrap', str(ROOT/'rootfs/usr/libexec/dcore/network-bootstrap')).exec_module(network)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root/'etc/network'
        self.config.mkdir(parents=True)
        self.state = self.root/'var/network-bootstrap.done'
        self.net = self.root/'sys/net'
        self.net.mkdir(parents=True)
        self.run = Mock(return_value=subprocess.CompletedProcess([], 0))

    def interface(self, name, mac='02:11:22:33:44:55', carrier='1', physical=True, wireless=False, assigned='0'):
        link = self.net/name
        link.mkdir()
        for filename, value in (('address',mac), ('carrier',carrier), ('type','1'), ('addr_assign_type',assigned)):
            (link/filename).write_text(value)
        if physical: (link/'device').mkdir()
        if wireless: (link/'wireless').mkdir()
        return link

    def invoke(self, sleep=None):
        network.bootstrap(self.config, self.state, self.net, self.run, sleep or Mock(side_effect=AssertionError('Unexpected wait')))

    def test_existing_configuration_including_masks_and_dropins_permanently_skips(self):
        for kind in ('file','mask','dropin'):
            with self.subTest(kind=kind):
                entry = self.config/'custom.network'
                if kind == 'file': entry.write_text('[Network]\nAddress=10.0.0.2/24\n')
                elif kind == 'mask': entry.symlink_to('/nonexistent-test-mask')
                else: entry.mkdir()
                self.invoke()
                self.assertTrue(self.state.exists())
                self.run.assert_not_called()
                if kind == 'dropin': entry.rmdir()
                else: entry.unlink()
                self.invoke()  # Removing existing configuration does not rearm.
                self.assertFalse((self.config/'10-dcore-dhcp.network').exists())
                self.state.unlink()

    def test_selects_first_connected_physical_ethernet_and_matches_mac(self):
        self.interface('br0', physical=False)
        self.interface('eth0', carrier='0')
        self.interface('wlan0', wireless=True)
        self.interface('eth2', mac='02:22:22:22:22:22')
        self.interface('eth1', mac='02:11:11:11:11:11')
        self.invoke()
        text = (self.config/'10-dcore-dhcp.network').read_text()
        self.assertIn('MACAddress=02:11:11:11:11:11', text)
        self.assertIn('DHCP=yes', text)
        self.assertIn('IPv6AcceptRA=yes', text)
        self.assertIn('RequiredForOnline=no', text)
        self.assertIn('UseDomains=yes', text)
        self.assertEqual(self.run.call_args.args[0], ['/usr/bin/networkctl', 'reconfigure', 'eth1'])
        self.assertFalse(any('br0' in call.args[0] or 'wlan0' in call.args[0] for call in self.run.call_args_list))
        self.run.reset_mock()
        (self.config/'10-dcore-dhcp.network').unlink()
        self.invoke()
        self.run.assert_not_called()

    def test_cable_connected_later_during_first_boot_is_configured(self):
        link = self.interface('enp1s0', carrier='0')
        def connect(_):
            self.assertTrue(self.state.exists())
            (link/'carrier').write_text('1')
        self.invoke(connect)
        self.assertTrue((self.config/'10-dcore-dhcp.network').exists())
        self.assertEqual(sum('ip' == Path(call.args[0][0]).name for call in self.run.call_args_list), 1)

    def test_admin_config_added_while_waiting_is_preserved(self):
        self.interface('eth0', carrier='0')
        def configure(_):
            (self.config/'admin.network').write_text('custom configuration')
        self.invoke(configure)
        self.assertEqual((self.config/'admin.network').read_text(), 'custom configuration')
        self.assertFalse((self.config/'10-dcore-dhcp.network').exists())
        self.assertFalse(any('networkctl' in command.args[0][0] for command in self.run.call_args_list))

    def test_shutdown_before_cable_does_not_restart_discovery(self):
        self.interface('eth0', carrier='0')
        def shutdown(_): raise InterruptedError('Simulate reboot')
        with self.assertRaises(InterruptedError): self.invoke(shutdown)
        self.run.reset_mock()
        self.invoke()
        self.run.assert_not_called()

    def test_randomized_or_invalid_addresses_are_not_selected(self):
        self.interface('eth0', assigned='1')
        self.interface('eth1', mac='not-a-mac')
        self.interface('eth2', mac='00:00:00:00:00:00')
        self.assertEqual(list(network.interfaces(self.net)), [])


if __name__ == '__main__': unittest.main(verbosity=2)
