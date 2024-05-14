# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2021 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import pprint
import ssl
from ipaddress import ip_interface
from socket import gaierror
from urllib.parse import unquote

from packaging import version
import openstack

from module.sources.common.source_base import SourceBase
from module.sources.openstack.config import OpenStackConfig
from module.common.logging import get_logger, DEBUG3
from module.common.misc import grab, dump, get_string_or_none
from module.common.support import normalize_mac_address
from module.netbox.inventory import NetBoxInventory
from module.netbox import *

log = get_logger()


# noinspection PyTypeChecker
class OpenStackHandler(SourceBase):
    """
    Source class to import data from a Openstack instance and add/update NetBox objects based on gathered information
    """

    dependent_netbox_objects = [
        NBTag,
        NBManufacturer,
        NBDeviceType,
        NBPlatform,
        NBClusterType,
        NBClusterGroup,
        NBDeviceRole,
        NBSite,
        NBCluster,
        NBDevice,
        NBVM,
        NBVMInterface,
        NBInterface,
        NBIPAddress,
        NBPrefix,
        NBTenant,
        NBVRF,
        NBVLAN,
        NBCustomField
    ]

    source_type = "openstack"

    # internal vars
    session = None
    tag_session = None

    site_name = None

    def __init__(self, name=None):

        if name is None:
            raise ValueError(f"Invalid value for attribute 'name': '{name}'.")

        self.inventory = NetBoxInventory()
        self.name = name

        # parse settings
        settings_handler = OpenStackConfig()
        settings_handler.source_name = self.name
        self.settings = settings_handler.parse()

        self.set_source_tag()
        self.site_name = f"OpenStack: {name}"

        if self.settings.enabled is False:
            log.info(f"Source '{name}' is currently disabled. Skipping")
            return

        self.create_openstack_session()

        if self.session is None:
            log.info(f"Source '{name}' is currently unavailable. Skipping")
            return

        self.init_successful = True
        self.permitted_clusters = dict()
        self.cluster_host_map = dict()
        self.volume_map = dict()
        self.processed_host_names = dict()
        self.processed_vm_names = dict()
        self.processed_vm_uuid = list()
        self.object_cache = dict()
        self.parsing_vms_the_first_time = True

    def create_openstack_session(self):
        """
        Initialize session with OpenStack

        Returns
        -------
        bool: if initialization was successful or not
        """

        if self.session is not None:
            return True

        log.debug(f"Starting OpenStack connection to '{self.settings.auth_url}'")

        ssl_context = ssl.create_default_context()
        if self.settings.validate_tls_certs is False:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        try:
            self.session = openstack.connect(
                auth_url=self.settings.auth_url,
                project_name=self.settings.project,
                username=self.settings.username,
                password=self.settings.password,
                region_name=self.settings.region,
                user_domain_name=self.settings.user_domain,
                project_domain_name=self.settings.project_domain,
                app_name='netbox-sync',
                app_version='0.1',
            )

        except (gaierror, OSError) as e:
            log.error(
                f"Unable to connect to OpenStack instance '{self.settings.auth_url}' on port {self.settings.port}. "
                f"Reason: {e}"
            )
            return False
        except Exception as e:
            log.error(f"Unable to connect to OpenStack instance '{self.settings.auth_url}' on port {self.settings.port}. {e.msg}")
            return False

        log.info(f"Successfully connected to OpenStack '{self.settings.auth_url}'")

        return True

    def apply(self):
        """
        Main source handler method. This method is called for each source from "main" program
        to retrieve data from it source and apply it to the NetBox inventory.

        Every update of new/existing objects fot this source has to happen here.
        """

        log.info(f"Query data from OpenStack: '{self.settings.auth_url}'")

        """
        Mapping of object type keywords to view types and handlers

        iterate over all VMs twice.

        To handle VMs with the same name in a cluster we first
        iterate over all VMs and look only at the active ones
        and sync these first.
        Then we iterate a second time to catch the rest.

        This has been implemented to support migration scenarios
        where you create the same machines with a different setup
        like a new version or something. This way NetBox will be
        updated primarily with the actual active VM data.
        """

        self.inventory.add_update_object(NBClusterGroup, data={"name": self.settings.group_name}, source=self)

        availability_zones = self.session.compute.availability_zones(details=True)
        for availability_zone in availability_zones:
            self.add_cluster(availability_zone)

        hypervisors = self.session.compute.hypervisors(details=True)
        for hypervisor in hypervisors:
            self.add_host(hypervisor)

        volumes = self.session.block_storage.volumes(details=True, all_projects=True)
        for volume in volumes:
            self.add_volume(volume)

        servers = self.session.compute.servers(details=True, all_projects=True)
        for server in servers:
            self.add_virtual_machine(server)

        self.update_basic_data()

    @staticmethod
    def passes_filter(name, include_filter, exclude_filter):
        """
        checks if object name passes a defined object filter.

        Parameters
        ----------
        name: str
            name of the object to check
        include_filter: regex object
            regex object of include filter
        exclude_filter: regex object
            regex object of exclude filter

        Returns
        -------
        bool: True if all filter passed, otherwise False
        """

        # first includes
        if include_filter is not None and not include_filter.match(name):
            log.debug(f"Object '{name}' did not match include filter '{include_filter.pattern}'. Skipping")
            return False

        # second excludes
        if exclude_filter is not None and exclude_filter.match(name):
            log.debug(f"Object '{name}' matched exclude filter '{exclude_filter.pattern}'. Skipping")
            return False

        return True

    def get_site_name(self, object_type, object_name, cluster_name=""):
        """
        Return a site name for a NBCluster or NBDevice depending on config options
        host_site_relation and cluster_site_relation

        Parameters
        ----------
        object_type: (NBCluster, NBDevice)
            object type to check site relation for
        object_name: str
            object name to check site relation for
        cluster_name: str
            cluster name of NBDevice to check for site name

        Returns
        -------
        str: site name if a relation was found
        """

        if object_type not in [NBCluster, NBDevice]:
            raise ValueError(f"Object must be a '{NBCluster.name}' or '{NBDevice.name}'.")

        log.debug2(f"Trying to find site name for {object_type.name} '{object_name}'")

        # check if site was provided in config
        relation_name = "host_site_relation" if object_type == NBDevice else "cluster_site_relation"

        site_name = self.get_object_relation(object_name, relation_name)

        if object_type == NBDevice and site_name is None:
            site_name = self.get_site_name(NBCluster, cluster_name)
            if site_name is not None:
                log.debug2(f"Found a matching cluster site for {object_name}, using site '{site_name}'")

        # set default site name
        if site_name is None:
            site_name = self.site_name
            log.debug(f"No site relation for '{object_name}' found, using default site '{site_name}'")

        return site_name

    def get_object_based_on_macs(self, object_type, mac_list=None):
        """
        Try to find a NetBox object based on list of MAC addresses.

        Iterate over all interfaces of this object type and compare MAC address with list of desired MAC
        addresses. If match was found store related machine object and count every correct match.

        If exactly one machine with matching interfaces was found then this one will be returned.

        If two or more machines with matching MACs are found compare the two machines with
        the highest amount of matching interfaces. If the ration of matching interfaces
        exceeds 2.0 then the top matching machine is chosen as desired object.

        If the ration is below 2.0 then None will be returned. The probability is too low that
        this one is the correct one.

        None will also be returned if no machine was found at all.

        Parameters
        ----------
        object_type: (NBDevice, NBVM)
            type of NetBox device to find in inventory
        mac_list: list
            list of MAC addresses to compare against NetBox interface objects

        Returns
        -------
        (NBDevice, NBVM, None): object instance of found device, otherwise None
        """

        object_to_return = None

        if object_type not in [NBDevice, NBVM]:
            raise ValueError(f"Object must be a '{NBVM.name}' or '{NBDevice.name}'.")

        if mac_list is None or not isinstance(mac_list, list) or len(mac_list) == 0:
            return

        interface_typ = NBInterface if object_type == NBDevice else NBVMInterface

        objects_with_matching_macs = dict()
        matching_object = None

        for interface in self.inventory.get_all_items(interface_typ):

            if grab(interface, "data.mac_address") in mac_list:

                matching_object = grab(interface, f"data.{interface.secondary_key}")
                if not isinstance(matching_object, (NBDevice, NBVM)):
                    continue

                log.debug2("Found matching MAC '%s' on %s '%s'" %
                           (grab(interface, "data.mac_address"), object_type.name,
                            matching_object.get_display_name(including_second_key=True)))

                if objects_with_matching_macs.get(matching_object) is None:
                    objects_with_matching_macs[matching_object] = 1
                else:
                    objects_with_matching_macs[matching_object] += 1

        # try to find object based on amount of matching MAC addresses
        num_devices_witch_matching_macs = len(objects_with_matching_macs.keys())

        if num_devices_witch_matching_macs == 1 and isinstance(matching_object, (NBDevice, NBVM)):

            log.debug2("Found one %s '%s' based on MAC addresses and using it" %
                       (object_type.name, matching_object.get_display_name(including_second_key=True)))

            object_to_return = list(objects_with_matching_macs.keys())[0]

        elif num_devices_witch_matching_macs > 1:

            log.debug2(f"Found {num_devices_witch_matching_macs} {object_type.name}s with matching MAC addresses")

            # now select the two top matches
            first_choice, second_choice = \
                sorted(objects_with_matching_macs, key=objects_with_matching_macs.get, reverse=True)[0:2]

            first_choice_matches = objects_with_matching_macs.get(first_choice)
            second_choice_matches = objects_with_matching_macs.get(second_choice)

            log.debug2(f"The top candidate {first_choice.get_display_name()} with {first_choice_matches} matches")
            log.debug2(f"The second candidate {second_choice.get_display_name()} with {second_choice_matches} matches")

            # get ratio between
            matching_ration = first_choice_matches / second_choice_matches

            # only pick the first one if the ration exceeds 2
            if matching_ration >= 2.0:
                log.debug2(f"The matching ratio of {matching_ration} is high enough "
                           f"to select {first_choice.get_display_name()} as desired {object_type.name}")
                object_to_return = first_choice
            else:
                log.debug2("Both candidates have a similar amount of "
                           "matching interface MAC addresses. Using NONE of them!")

        return object_to_return

    def get_object_based_on_primary_ip(self, object_type, primary_ip4=None, primary_ip6=None):
        """
        Try to find a NBDevice or NBVM based on the primary IP address. If an exact
        match was found the device/vm object will be returned immediately without
        checking of the other primary IP address (if defined).

        Parameters
        ----------
        object_type: (NBDevice, NBVM)
            object type to look for
        primary_ip4: str
            primary IPv4 address of object to find
        primary_ip6: str
            primary IPv6 address of object to find

        Returns
        -------

        """

        def _matches_device_primary_ip(device_primary_ip, ip_needle):

            ip = None
            if device_primary_ip is not None and ip_needle is not None:
                if isinstance(device_primary_ip, dict):
                    ip = grab(device_primary_ip, "address")

                elif isinstance(device_primary_ip, int):
                    ip = self.inventory.get_by_id(NBIPAddress, nb_id=device_primary_ip)
                    ip = grab(ip, "data.address")

                if ip is not None and ip.split("/")[0] == ip_needle:
                    return True

            return False

      if object_type not in [NBDevice, NBVM]:
            raise ValueError(f"Object must be a '{NBVM.name}' or '{NBDevice.name}'.")

        if primary_ip4 is None and primary_ip6 is None:
            return

        if primary_ip4 is not None:
            primary_ip4 = str(primary_ip4).split("/")[0]

        if primary_ip6 is not None:
            primary_ip6 = str(primary_ip6).split("/")[0]

        for device in self.inventory.get_all_items(object_type):

            if _matches_device_primary_ip(grab(device, "data.primary_ip4"), primary_ip4) is True:
                log.debug2(f"Found existing host '{device.get_display_name()}' "
                           f"based on the primary IPv4 '{primary_ip4}'")
                return device

            if _matches_device_primary_ip(grab(device, "data.primary_ip6"), primary_ip6) is True:
                log.debug2(f"Found existing host '{device.get_display_name()}' "
                           f"based on the primary IPv6 '{primary_ip6}'")
                return device

    def get_object_relation(self, name, relation, fallback=None):
        """

        Parameters
        ----------
        name: str
            name of the object to find a relation for
        relation: str
            name of the config variable relation (i.e: vm_tag_relation)
        fallback: str
            fallback string if no relation matched

        Returns
        -------
        data: str, list, None
            string of matching relation or list of matching tags
        """

        resolved_list = list()
        for single_relation in grab(self.settings, relation, fallback=list()):
            object_regex = single_relation.get("object_regex")
            match_found = False
            if object_regex.match(name):
                resolved_name = single_relation.get("assigned_name")
                log.debug2(f"Found a matching {relation} '{resolved_name}' ({object_regex.pattern}) for {name}")
                resolved_list.append(resolved_name)
                match_found = True

            # special cluster condition
            if match_found is False and grab(f"{relation}".split("_"), "0") == "cluster":

                stripped_name = "/".join(name.split("/")[1:])
                if object_regex.match(stripped_name):

                    resolved_name = single_relation.get("assigned_name")
                    log.debug2(f"Found a matching {relation} '{resolved_name}' ({object_regex.pattern}) "
                               f"for {stripped_name}")
                    resolved_list.append(resolved_name)

        if grab(f"{relation}".split("_"), "1") == "tag":
            return resolved_list

        else:
            resolved_name = fallback
            if len(resolved_list) >= 1:
                resolved_name = resolved_list[0]
                if len(resolved_list) > 1:
                    log.debug(f"Found {len(resolved_list)} matches for {name} in {relation}."
                              f" Using first on: {resolved_name}")

            return resolved_name

    def get_cluster_for_host(self, hostname):
        for cluster, hosts in self.cluster_host_map.items():
            if hostname in hosts:
                return cluster
        return None

    def add_device_vm_to_inventory(self, object_type, object_data, pnic_data=None, vnic_data=None,
                                   nic_ips=None, p_ipv4=None, p_ipv6=None):
        """
        Add/update device/VM object in inventory based on gathered data.

        Try to find object first based on the object data, interface MAC addresses and primary IPs.
            1. try to find by name and cluster/site
            2. try to find by mac addresses interfaces
            3. try to find by primary IP

        IP addresses for each interface are added here as well. First they will be checked and added
        if all checks pass. For each IP address a matching IP prefix will be searched for. First we
        look for longest matching IP Prefix in the same site. If this failed we try to find the longest
        matching global IP Prefix.

        If a IP Prefix was found then we try to get the VRF and VLAN for this prefix. Now we compare
        if interface VLAN and prefix VLAN match up and warn if they don't. Then we try to add data to
        the IP address if not already set:

            add prefix VRF if VRF for this IP is undefined
            add tenant if tenant for this IP is undefined
                1. try prefix tenant
                2. if prefix tenant is undefined try VLAN tenant

        And we also set primary IP4/6 for this object depending on the "set_primary_ip" setting.

        If a IP address is set as primary IP for another device then using this IP on another
        device will be rejected by NetBox.

        Setting "always":
            check all NBDevice and NBVM objects if this IP address is set as primary IP to any
            other object then this one. If we found another object, then we unset the primary_ip*
            for the found object and assign it to this object.

            This setting will also reset the primary IP if it has been changed in NetBox

        Setting "when-undefined":
            Will set the primary IP for this object if primary_ip4/6 is undefined. Will cause a
            NetBox error if IP has been assigned to a different object as well

        Setting "never":
            Well, the attribute primary_ip4/6 will never be touched/changed.

        Parameters
        ----------
        object_type: (NBDevice, NBVM)
            NetBoxObject sub class of object to add
        object_data: dict
            data of object to add/update
        pnic_data: dict
            data of physical interfaces of this object, interface name as key
        vnic_data: dict
            data of virtual interfaces of this object, interface name as key
        nic_ips: dict
            dict of ips per interface of this object, interface name as key
        p_ipv4: str
            primary IPv4 as string including netmask/prefix
        p_ipv6: str
            primary IPv6 as string including netmask/prefix

        """

        if object_type not in [NBDevice, NBVM]:
            raise ValueError(f"Object must be a '{NBVM.name}' or '{NBDevice.name}'.")

        if log.level == DEBUG3:

            log.debug3("function: add_device_vm_to_inventory")
            log.debug3(f"Object type {object_type}")
            pprint.pprint(object_data)
            pprint.pprint(pnic_data)
            pprint.pprint(vnic_data)
            pprint.pprint(nic_ips)
            pprint.pprint(p_ipv4)
            pprint.pprint(p_ipv6)

        # check existing Devices for matches
        log.debug2(f"Trying to find a {object_type.name} based on the collected name, cluster, IP and MAC addresses")

        device_vm_object = self.inventory.get_by_data(object_type, data=object_data)

        if device_vm_object is not None:
            log.debug2("Found a exact matching %s object: %s" %
                       (object_type.name, device_vm_object.get_display_name(including_second_key=True)))

        # keep searching if no exact match was found
        else:

            log.debug2(f"No exact match found. Trying to find {object_type.name} based on MAC addresses")

            # on VMs vnic data is used, on physical devices pnic data is used
            mac_source_data = vnic_data if object_type == NBVM else pnic_data

            nic_macs = [x.get("mac_address") for x in mac_source_data.values() if x.get("mac_address") is not None]

            device_vm_object = self.get_object_based_on_macs(object_type, nic_macs)

        if device_vm_object is not None:
            log.debug2("Found a matching %s object: %s" %
                       (object_type.name, device_vm_object.get_display_name(including_second_key=True)))

        # keep looking for devices with the same primary IP
        else:

            log.debug2(f"No match found. Trying to find {object_type.name} based on primary IP addresses")

            device_vm_object = self.get_object_based_on_primary_ip(object_type, p_ipv4, p_ipv6)

        if device_vm_object is None:
            object_name = object_data.get(object_type.primary_key)
            log.debug(f"No existing {object_type.name} object for {object_name}. Creating a new {object_type.name}.")
            device_vm_object = self.inventory.add_object(object_type, data=object_data, source=self)
        else:
            device_vm_object.update(data=object_data, source=self)

        # update role according to config settings
        object_name = object_data.get(object_type.primary_key)
        role_name = self.get_object_relation(object_name,
                                             "host_role_relation" if object_type == NBDevice else "vm_role_relation")

        if object_type == NBDevice:
            if role_name is None:
                role_name = "Server"
            device_vm_object.update(data={"device_role": {"name": role_name}})
        if object_type == NBVM and role_name is not None:
            device_vm_object.update(data={"role": {"name": role_name}})

        # compile all nic data into one dictionary
        if object_type == NBVM:
            nic_data = vnic_data
        else:
            nic_data = {**pnic_data, **vnic_data}

        # map interfaces of existing object with discovered interfaces
        nic_object_dict = self.map_object_interfaces_to_current_interfaces(device_vm_object, nic_data)

        if object_data.get("status", "") == "active" and (nic_ips is None or len(nic_ips.keys()) == 0):
            log.debug(f"No IP addresses for '{object_name}' found!")

        primary_ipv4_object = None
        primary_ipv6_object = None

        if p_ipv4 is not None:
            try:
                primary_ipv4_object = ip_interface(p_ipv4)
            except ValueError:
                log.error(f"Primary IPv4 ({p_ipv4}) does not appear to be a valid IP address (needs included suffix).")

        if p_ipv6 is not None:
            try:
                primary_ipv6_object = ip_interface(p_ipv6)
            except ValueError:
                log.error(f"Primary IPv6 ({p_ipv6}) does not appear to be a valid IP address (needs included suffix).")

        for int_name, int_data in nic_data.items():

            # add/update interface with retrieved data
            nic_object, ip_address_objects = self.add_update_interface(nic_object_dict.get(int_name), device_vm_object,
                                                                       int_data, nic_ips.get(int_name, list()))

            # add all interface IPs
            for ip_object in ip_address_objects:

                if ip_object is None:
                    continue

                ip_interface_object = ip_interface(grab(ip_object, "data.address"))

                # continue if address is not a primary IP
                if ip_interface_object not in [primary_ipv4_object, primary_ipv6_object]:
                    continue

                # set/update/remove primary IP addresses
                set_this_primary_ip = False
                ip_version = ip_interface_object.ip.version
                if self.settings.set_primary_ip == "always":

                    for object_type in [NBDevice, NBVM]:

                        # new IPs don't need to be removed from other devices/VMs
                        if ip_object.is_new is True:
                            break

                        for devices_vms in self.inventory.get_all_items(object_type):

                            # device has no primary IP of this version
                            this_primary_ip = grab(devices_vms, f"data.primary_ip{ip_version}")

                            # we found this exact object
                            if devices_vms == device_vm_object:
                                continue

                            # device has the same object assigned
                            if this_primary_ip == ip_object:
                                devices_vms.unset_attribute(f"primary_ip{ip_version}")

                    set_this_primary_ip = True

                elif self.settings.set_primary_ip != "never" and \
                        grab(device_vm_object, f"data.primary_ip{ip_version}") is None:
                    set_this_primary_ip = True

                if set_this_primary_ip is True:

                    log.debug(f"Setting IP '{grab(ip_object, 'data.address')}' as primary IPv{ip_version} for "
                              f"'{device_vm_object.get_display_name()}'")
                    device_vm_object.update(data={f"primary_ip{ip_version}": ip_object})

        return

    def add_object_to_cache(self, obj_type, key, netbox_object):

        if None in [type, key, netbox_object]:
            return

        # noinspection PyBroadException
        try:
            vm_class_name = obj_type.__class__.__name__
        except Exception:
            return

        if self.object_cache.get(vm_class_name) is None:
            self.object_cache[vm_class_name] = dict()

        self.object_cache[vm_class_name][key] = netbox_object

    def get_object_from_cache(self, obj_type, key):

        if obj_type is None or key is None:
            return

        # noinspection PyBroadException
        try:
            vm_class_name = obj_type.__class__.__name__
        except Exception:
            return

        if self.object_cache.get(vm_class_name) is None:
            return

        return self.object_cache[vm_class_name].get(key)

    def add_cluster(self, obj):
        """
        Add a OpenStack Availability Zone as a NBCluster to NetBox. Cluster name is checked against
        cluster_include_filter and cluster_exclude_filter config setting. Also adds
        cluster and site_name to "self.permitted_clusters" so hosts and VMs can be
        checked if they are part of a permitted cluster.

        Parameters
        ----------
        obj: openstack.compute.v2.availability_zone.AvailabilityZone
            cluster to add
        """

        name = get_string_or_none(grab(obj, "name"))
        group = self.inventory.get_by_data(NBClusterGroup, data={"name": self.settings.group_name})

        if name is None or group is None:
            return

        group_name = grab(group, "data.name")

        log.debug(f"Parsing OpenStack AZ: {name}")

        if self.passes_filter(name, self.settings.cluster_include_filter,
                              self.settings.cluster_exclude_filter) is False:
            return

        site_name = self.get_site_name(NBCluster, name)

        data = {
            "name": name,
            "type": {"name": "Openstack"},
            "group": group,
            "site": {"name": site_name}
        }

        tenant_name = self.get_object_relation(name, "cluster_tenant_relation")
        if tenant_name is not None:
            data["tenant"] = {"name": tenant_name}

        # try to find cluster including cluster group
        log.debug2("Trying to find a matching existing cluster")
        cluster_object = None
        fallback_cluster_object = None
        for cluster_candidate in self.inventory.get_all_items(NBCluster):
            if grab(cluster_candidate, "data.name") != name:
                continue

            # try to find a cluster with matching site
            if cluster_candidate.get_site_name() == site_name:
                cluster_object = cluster_candidate
                log.debug2("Found an existing cluster where 'name' and 'site' are matching")
                break

            if grab(cluster_candidate, "data.group") is not None and \
                    grab(cluster_candidate, "data.group.data.name") == group_name:
                cluster_object = cluster_candidate
                log.debug2("Found an existing cluster where 'name' and 'cluster group' are matching")
                break

            if grab(cluster_candidate, "data.tenant") is not None and \
                    tenant_name is not None and \
                    grab(cluster_candidate, "data.tenant.data.name") == tenant_name:
                cluster_object = cluster_candidate
                log.debug2("Found an existing cluster where 'name' and 'tenant' are matching")
                break

            # if only the name matches and there are multiple cluster with the same name we choose the first
            # cluster returned from netbox. This needs to be done to not ignore possible matches in one of
            # the next iterations
            if fallback_cluster_object is None:
                fallback_cluster_object = cluster_candidate

        if cluster_object is None and fallback_cluster_object is not None:
            log.debug2(f"Found an existing cluster where 'name' "
                       f"matches (NetBox id: {fallback_cluster_object.get_nb_reference()})")
            cluster_object = fallback_cluster_object

        if cluster_object is not None:
            cluster_object.update(data=data, source=self)
        else:
            cluster_object = self.inventory.add_update_object(NBCluster, data=data, source=self)

        self.add_object_to_cache(NBCluster, name, cluster_object)

        self.cluster_host_map[name] = list()
        for host in obj.hosts:
            self.cluster_host_map[name].append(host)

        self.permitted_clusters[name] = site_name

    def add_host(self, obj):
        """
        Parse a Openstack host to NetBox once all data is gathered.

        First host is filtered:
             host has a cluster and is it permitted
             was host with same name and site already parsed
             does the host pass the host_include_filter and host_exclude_filter

        Then all necessary host data will be collected.

        Primary IPv4/6 will be determined by 'host_ip' value

        Parameters
        ----------
        obj: Hypervisor
            host object to parse
        """

        name = get_string_or_none(grab(obj, "name"))

        if name is not None and self.settings.strip_host_domain_name is True:
            name = name.split(".")[0]

        # parse data
        log.debug(f"Parsing Openstack host: {name}")

        #
        # Filtering
        #

        # manage site and cluster
        short_name = get_string_or_none(obj.service_details["host"])
        cluster_name = self.get_cluster_for_host(short_name)

        if cluster_name is None:
            log.error(f"Requesting cluster for host '{name}' failed. Skipping.")
            return

        if log.level == DEBUG3:
            try:
                log.info("Cluster data")
                dump(obj.service_details.to_dict())
            except Exception as e:
                log.error(e)

        if self.permitted_clusters.get(cluster_name) is None:
            log.debug(f"Host '{name}' is not part of a permitted cluster. Skipping")
            return

        # get cluster object
        nb_cluster_object = self.get_object_from_cache(NBCluster, cluster_name)

        # get a site for this host
        site_name = self.get_site_name(NBDevice, name, cluster_name)

        if name in self.processed_host_names.get(site_name, list()):
            log.warning(f"Host '{name}' for site '{site_name}' already parsed. "
                        "Make sure to use unique host names. Skipping")
            return

        # add host to processed list
        if self.processed_host_names.get(site_name) is None:
            self.processed_host_names[site_name] = list()

        self.processed_host_names[site_name].append(name)

        # filter hosts by name
        if self.passes_filter(name, self.settings.host_include_filter, self.settings.host_exclude_filter) is False:
            return

        #
        # Collecting data
        #

        # collect all necessary data
        manufacturer = None
        model = None
        product_name = get_string_or_none(obj.hypervisor_type)
        product_version = get_string_or_none(obj.hypervisor_version)
        platform = f"{product_name} {product_version}"

        # if the device vendor/model cannot be retrieved (due to problem on the host),
        # set a dummy value so the host still gets synced
        if manufacturer is None:
            manufacturer = "Generic Vendor"
        if model is None:
            model = "Generic Model"

        # get status
        status = "offline"
        if get_string_or_none(obj.status) == "enabled":
            status = "active"

        # get host_tenant_relation
        tenant_name = self.get_object_relation(name, "host_tenant_relation")

        # get host_tag_relation
        host_tags = self.get_object_relation(name, "host_tag_relation")

        # prepare host data model
        host_data = {
            "name": name,
            "device_type": {
                "model": model,
                "manufacturer": {
                    "name": manufacturer
                }
            },
            "site": {"name": site_name},
            "cluster": nb_cluster_object,
            "status": status
        }

        # add data if present
        if platform is not None:
            host_data["platform"] = {"name": platform}
        if tenant_name is not None:
            host_data["tenant"] = {"name": tenant_name}
        if len(host_tags) > 0:
            host_data["tags"] = host_tags

        pnic_data_dict = dict()
        pnic_data = {
                "name": "eth0",
                "device": None,
                "enabled": True,
                "type": "10gbase-t"
        }
        pnic_data_dict["eth0"] = pnic_data

        ip_addr = obj.host_ip
        prefix = None
        matched_prefix = self.return_longest_matching_prefix_for_ip(ip_interface(ip_addr))
        if matched_prefix is not None:
            prefix = matched_prefix.data["prefix"].prefixlen
            ip_addr = f"{ip_addr}/{prefix}"

        vnic_ips = dict()
        vnic_ips["eth0"] = list()
        vnic_ips["eth0"].append(ip_addr)

        host_primary_ip4 = ip_addr
        host_primary_ip6 = None

        # add host to inventory
        self.add_device_vm_to_inventory(NBDevice, object_data=host_data, pnic_data=pnic_data_dict,
                                        vnic_data=dict(), nic_ips=vnic_ips,
                                        p_ipv4=host_primary_ip4, p_ipv6=host_primary_ip6)

        return

    def add_volume(self, obj):
        """
        Parse OpenStack volume and store in in a map.
        """

        id = obj.id
        size = obj.size

        self.volume_map[id] = size

    def add_virtual_machine(self, obj):
        """
        Parse a OpenStack VM add to NetBox once all data is gathered.

        Parameters
        ----------
        obj: openstack.compute.v2.server.Server
            virtual machine object to parse
        """

        name = get_string_or_none(grab(obj, "name"))

        if name is not None and self.settings.strip_vm_domain_name is True:
            name = name.split(".")[0]

        if self.settings.set_vm_name_to_uuid:
            display_name = name
            name = get_string_or_none(obj.id)

        log.debug(f"Parsing OpenStack VM: {name}")

        # get VM power state
        status = "active" if get_string_or_none(obj.status) == "ACTIVE" else "offline"

        cluster_name = get_string_or_none(obj.availability_zone)

        # honor strip_host_domain_name
        if cluster_name is not None and self.settings.strip_host_domain_name is True:
            cluster_name = cluster_name.split(".")[0]

        # check VM cluster
        if cluster_name is None:
            log.error(f"Requesting cluster for Virtual Machine '{name}' failed. Skipping.")
            return

        elif self.permitted_clusters.get(cluster_name) is None:
            log.debug(f"Virtual machine '{name}' is not part of a permitted cluster. Skipping")
            return

        nb_cluster_object = self.get_object_from_cache(NBCluster, cluster_name)

        if name in self.processed_vm_names.get(cluster_name, list()):
            log.warning(f"Virtual machine '{name}' for cluster '{cluster_name}' already parsed. "
                        "Make sure to use unique VM names. Skipping")
            return

        # add host to processed list
        if self.processed_vm_names.get(cluster_name) is None:
            self.processed_vm_names[cluster_name] = list()

        self.processed_vm_names[cluster_name].append(name)

        # filter VMs by name
        if self.passes_filter(name, self.settings.vm_include_filter, self.settings.vm_exclude_filter) is False:
            return

        #
        # Collect data
        #

        site_name = self.get_site_name(NBCluster, cluster_name)
        # first check against vm_platform_relation
        platform = None
        if self.settings.skip_vm_platform is False:
            platform = get_string_or_none(obj.flavor["original_name"])

            if platform is not None:
                platform = self.get_object_relation(platform, "vm_platform_relation", fallback=platform)

        disk = 0
        for volume in obj.attached_volumes:
            volid = volume["id"]
            size = self.volume_map[volid]
            disk += int(size)

        annotation = None
        if self.settings.skip_vm_comments is False:
            if not self.settings.set_vm_name_to_uuid:
                annotation = get_string_or_none(obj.id)

        # assign vm_tenant_relation
        tenant_name = self.get_object_relation(name, "vm_tenant_relation")

        # is the data incorrect or mismatched? DATA IS INCORRECT. Does not include memory, vcpus.
        # quiet the error from vcpu not being > 0.010...
        cpu = obj.flavor["vcpus"]
        if cpu == 0:
            cpu = 1

        vm_data = {
            "name": name,
            "cluster": nb_cluster_object,
            "status": status,
            "memory": obj.flavor["ram"],
            "vcpus": cpu,
            "disk": disk
        }

        # Add adaption for change in NetBox 3.3.0 VM model
        # issue: https://github.com/netbox-community/netbox/issues/10131#issuecomment-1225783758
        if version.parse(self.inventory.netbox_api_version) >= version.parse("3.3.0"):
            vm_data["site"] = {"name": site_name}
        if platform is not None:
            vm_data["platform"] = {"name": platform}
        if annotation is not None:
            vm_data["comments"] = annotation
        if tenant_name is not None:
            vm_data["tenant"] = {"name": tenant_name}

        if self.settings.set_vm_name_to_uuid:
            custom_field = self.add_update_custom_field({
                    "name": "openstack_vm_name",
                    "label": "name",
                    "content_types": "virtualization.virtualmachine",
                    "type": "text",
                    "description": f"Openstack '{self.name}' synced object attribute 'name'"
                })
            vm_data["custom_fields"] = {
                grab(custom_field, "data.name"): get_string_or_none(grab(obj, "name"))
            }

        vm_primary_ip4 = None
        vm_primary_ip6 = None
        vm_nic_dict = dict()
        nic_ips = dict()
        count = 0

        for network, addresses in obj.addresses.items():
            count += 1
            nic_ips[network] = list()
            for address in addresses:
                ip_addr = address["addr"]
                prefix = None

                matched_prefix = self.return_longest_matching_prefix_for_ip(ip_interface(ip_addr), site_name)
                if matched_prefix is None:
                    matched_prefix = self.return_longest_matching_prefix_for_ip(ip_interface(ip_addr))
                if matched_prefix is not None:
                    prefix = matched_prefix.data["prefix"].prefixlen
                    ip_addr = f"{ip_addr}/{prefix}"

                nic_ips[network].append(ip_addr)
                if int(address["version"]) == 4:
                    vm_primary_ip4 = ip_addr
                if int(address["version"]) == 6:
                    vm_primary_ip6 = ip_addr
                full_name = unquote(f"vNIC{count} ({network})")
                vm_nic_data = {
                    "name": full_name,
                    "virtual_machine": None,
                    "mac_address": normalize_mac_address(address["OS-EXT-IPS-MAC:mac_addr"]),
                    "description": full_name,
                    "enabled": True,
                }
                if self.settings.permitted_subnets.permitted(ip_addr, interface_name=full_name) is True:
                    vm_nic_dict[network] = vm_nic_data
                else:
                    log.debug(f"Virtual machine '{name}' address '{ip_addr}' is not valid to add. Skipping")

        # add VM to inventory
        self.add_device_vm_to_inventory(NBVM, object_data=vm_data, vnic_data=vm_nic_dict,
                                        nic_ips=nic_ips, p_ipv4=vm_primary_ip4, p_ipv6=vm_primary_ip6)

        return

    def update_basic_data(self):
        """

        Returns
        -------

        """

        # add source identification tag
        self.inventory.add_update_object(NBTag, data={
            "name": self.source_tag,
            "description": f"Marks objects synced from Openstack '{self.name}' "
                           f"({self.settings.auth_url}) to this NetBox Instance."
        })

        # update virtual site if present
        this_site_object = self.inventory.get_by_data(NBSite, data={"name": self.site_name})

        if this_site_object is not None:
            this_site_object.update(data={
                "name": self.site_name,
                "comments": "A default virtual site created to house objects "
                            "that have been synced from this Openstack instance "
                            "and have no predefined site assigned."
            })

        server_role_object = self.inventory.get_by_data(NBDeviceRole, data={"name": "Server"})

        if server_role_object is not None:
            role_data = {"name": "Server", "vm_role": True}
            if server_role_object.is_new is True:
                role_data["color"] = "9e9e9e"

            server_role_object.update(data=role_data)

# EOF
