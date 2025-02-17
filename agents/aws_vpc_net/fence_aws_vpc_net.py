#!@PYTHON@ -tt

import sys, re
import json
import atexit
import logging
import time
import requests

sys.path.append("@FENCEAGENTSLIBDIR@")

from fencing import *
from fencing import (
    run_delay,
    fail,
    fail_usage,
    EC_STATUS,
    EC_GENERIC_ERROR,
    SyslogLibHandler
)

try:
    import boto3
    from botocore.exceptions import ConnectionError, ClientError, EndpointConnectionError, NoRegionError
except ImportError:
    pass

# Logger configuration
logger = logging.getLogger()
logger.propagate = False
logger.setLevel(logging.INFO)
logger.addHandler(SyslogLibHandler())
logging.getLogger('botocore.vendored').propagate = False

status = {
    "running": "on",
    "stopped": "off",
    "pending": "unknown",
    "stopping": "unknown",
    "shutting-down": "unknown",
    "terminated": "unknown"
}

def get_power_status(conn, options):
    logger.debug("Starting status operation")
    try:
        instance_id = options["--plug"]
        ec2_client = conn.meta.client

        # Get the lastfence tag first
        lastfence_response = ec2_client.describe_tags(
            Filters=[
                {"Name": "resource-id", "Values": [instance_id]},
                {"Name": "key", "Values": ["lastfence"]}
            ]
        )

        # Helper function to check if security groups have been modified
        def check_sg_modifications():
            try:
                state, _, interfaces = get_instance_details(ec2_client, instance_id)
                if state == "running":  # Only check SGs if instance is running
                    sg_to_remove = options.get("--secg", "").split(",") if options.get("--secg") else []
                    if sg_to_remove:
                        # Check if all interfaces have had their security groups modified
                        all_interfaces_fenced = True
                        for interface in interfaces:
                            current_sgs = interface["SecurityGroups"]
                            if "--invert-sg-removal" in options:
                                # In keep_only mode, check if interface only has the specified groups
                                if sorted(current_sgs) != sorted(sg_to_remove):
                                    logger.debug(f"Interface {interface['NetworkInterfaceId']} still has different security groups")
                                    all_interfaces_fenced = False
                                    break
                            else:
                                # In remove mode, check if specified groups were removed
                                if any(sg in current_sgs for sg in sg_to_remove):
                                    logger.debug(f"Interface {interface['NetworkInterfaceId']} still has security groups that should be removed")
                                    all_interfaces_fenced = False
                                    break

                        if all_interfaces_fenced:
                            logger.debug("All interfaces have had their security groups successfully modified - considering instance fenced")
                            return True
            except Exception as e:
                logger.debug("Failed to check security group modifications: %s", e)
            return False

        # If --ignore-tag-write-failure is set, prioritize checking SG modifications
        if "--ignore-tag-write-failure" in options:
            logger.debug("--ignore-tag-write-failure is set, checking security group modifications first")
            if check_sg_modifications():
                logger.info("All interfaces are properly fenced based on security group state, ignoring tag state")
                return "off"
            logger.debug("Not all interfaces are fenced, proceeding with tag checks")
            # Only proceed with tag checks if we haven't determined state from SG modifications

        try:
            # If no lastfence tag exists, instance is not fenced
            if not lastfence_response["Tags"]:
                logger.debug("No lastfence tag found for instance %s - instance is not fenced", instance_id)
                return "on"

            lastfence_timestamp = lastfence_response["Tags"][0]["Value"]
        except Exception as e:
            if "--ignore-tag-write-failure" in options:
                logger.warning(f"Failed to check lastfence tag but continuing due to ignore-tag-write-failure: {str(e)}")
                # If we can't read tags but --ignore-tag-write-failure is set, rely on SG state
                return "on"  # Default to "on" to allow fence operation to proceed
            raise

        # Check for backup tags with pattern Original_SG_Backup_{instance_id}_*
        response = ec2_client.describe_tags(
            Filters=[
                {"Name": "resource-id", "Values": [instance_id]},
                {"Name": "key", "Values": [f"Original_SG_Backup_{instance_id}*"]}
            ]
        )

        # If no backup tags exist, instance is not fenced (unless --ignore-tag-write-failure handled above)
        if not response["Tags"]:
            logger.debug("No backup tags found for instance %s - instance is not fenced", instance_id)
            return "on"

        # Loop through backup tags to find matching timestamp
        for tag in response["Tags"]:
            try:
                backup_data = json.loads(tag["Value"])
                backup_timestamp = backup_data.get("t")  # Using shortened timestamp field

                if not backup_timestamp:
                    logger.debug("No timestamp found in backup data for tag %s", tag["Key"])
                    continue

                # Validate timestamps match
                if str(backup_timestamp) == str(lastfence_timestamp):
                    # Check if security groups were actually modified to confirm fencing
                    try:
                        state, _, interfaces = get_instance_details(ec2_client, instance_id)
                        if state == "running":  # Only check SGs if instance is running
                            sg_to_remove = options.get("--secg", "").split(",") if options.get("--secg") else []
                            if sg_to_remove:
                                # Check if all interfaces have had their security groups modified
                                all_interfaces_fenced = True
                                for interface in interfaces:
                                    current_sgs = interface["SecurityGroups"]
                                    if "--invert-sg-removal" in options:
                                        # In keep_only mode, check if interface only has the specified groups
                                        if sorted(current_sgs) != sorted(sg_to_remove):
                                            logger.debug(f"Interface {interface['NetworkInterfaceId']} still has different security groups")
                                            all_interfaces_fenced = False
                                            break
                                    else:
                                        # In remove mode, check if specified groups were removed
                                        if any(sg in current_sgs for sg in sg_to_remove):
                                            logger.debug(f"Interface {interface['NetworkInterfaceId']} still has security groups that should be removed")
                                            all_interfaces_fenced = False
                                            break

                                if all_interfaces_fenced:
                                    logger.debug("Found matching backup tag %s and verified all interfaces have SG changes - instance is fenced", tag["Key"])
                                    return "off"
                    except Exception as e:
                        logger.debug("Failed to check security group modifications: %s", e)
                        # If we can't verify SG changes but have matching tags, assume fenced for backward compatibility
                        logger.debug("Found matching backup tag %s but couldn't verify SG changes - assuming instance is fenced", tag["Key"])
                        return "off"

            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"Failed to parse backup data for tag {tag['Key']}: {str(e)}")
                continue

        logger.debug("No backup tags with matching timestamp found - instance is not fenced")
        return "on"

    except ClientError:
        fail_usage("Failed: Incorrect Access Key or Secret Key.")
    except EndpointConnectionError:
        fail_usage("Failed: Incorrect Region.")
    except IndexError:
        fail(EC_STATUS)
    except Exception as e:
        logger.error("Failed to get power status: %s", e)
        fail(EC_STATUS)

# Retrieve instance ID for self-check
def get_instance_id():
    """Retrieve the instance ID of the current EC2 instance."""
    try:
        token = requests.put(
            "http://169.254.169.254/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        ).content.decode("UTF-8")
        instance_id = requests.get(
            "http://169.254.169.254/latest/meta-data/instance-id",
            headers={"X-aws-ec2-metadata-token": token},
        ).content.decode("UTF-8")
        return instance_id
    except Exception as err:
        logger.error("Failed to retrieve instance ID for self-check: %s", err)
        return None


# Retrieve instance details
def get_instance_details(ec2_client, instance_id):
    """Retrieve instance details including state, VPC, interfaces, and attached SGs."""
    try:
        response = ec2_client.describe_instances(InstanceIds=[instance_id])
        instance = response["Reservations"][0]["Instances"][0]

        instance_state = instance["State"]["Name"]
        vpc_id = instance["VpcId"]
        network_interfaces = instance["NetworkInterfaces"]

        interfaces = []
        for interface in network_interfaces:
            try:
                interfaces.append(
                    {
                        "NetworkInterfaceId": interface["NetworkInterfaceId"],
                        "SecurityGroups": [sg["GroupId"] for sg in interface["Groups"]],
                    }
                )
            except KeyError as e:
                logger.error(f"Malformed interface data: {str(e)}")
                continue

        return instance_state, vpc_id, interfaces

    except ClientError as e:
        logger.error(f"AWS API error while retrieving instance details: {str(e)}")
        raise
    except IndexError as e:
        logger.error(f"Instance {instance_id} not found or no instances returned: {str(e)}")
        raise
    except KeyError as e:
        logger.error(f"Unexpected response format from AWS API: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error while retrieving instance details: {str(e)}")
        raise

# Check if we are the self-fencing node
def get_self_power_status(conn, instance_id):
    try:
        instance = conn.instances.filter(Filters=[{"Name": "instance-id", "Values": [instance_id]}])
        state = list(instance)[0].state["Name"]
        if state == "running":
            logger.debug(f"Captured my ({instance_id}) state and it {state.upper()} - returning OK - Proceeding with fencing")
            return "ok"
        else:
            logger.debug(f"Captured my ({instance_id}) state it is {state.upper()} - returning Alert - Unable to fence other nodes")
            return "alert"

    except ClientError:
        fail_usage("Failed: Incorrect Access Key or Secret Key.")
    except EndpointConnectionError:
        fail_usage("Failed: Incorrect Region.")
    except IndexError:
        return "fail"

# Create backup tags for each network interface
def create_backup_tag(ec2_client, instance_id, interfaces, timestamp):
    """Create tags on the instance to backup original security groups for each network interface.
    If the security groups list is too long, it will be split across multiple tags."""
    try:
        # Create tags for each network interface
        for idx, interface in enumerate(interfaces, 1):
            interface_id = interface["NetworkInterfaceId"]
            security_groups = interface["SecurityGroups"]

            # Initialize variables for chunking
            sg_chunks = []
            current_chunk = []

            # Strip 'sg-' prefix from all security groups first
            stripped_sgs = [sg[3:] if sg.startswith('sg-') else sg for sg in security_groups]

            for sg in stripped_sgs:
                # Create a test chunk with the new security group
                test_chunk = current_chunk + [sg]

                # Create a test backup object with this chunk
                test_backup = {
                    "n": {
                        "i": interface_id,
                        "s": test_chunk,
                        "c": {
                            "i": len(sg_chunks),
                            "t": 1  # Temporary value, will update later
                        }
                    },
                    "t": timestamp
                }

                # Check if adding this SG would exceed the character limit
                if len(json.dumps(test_backup)) > 254:
                    # Current chunk is full, add it to chunks and start a new one
                    if current_chunk:  # Only add if not empty
                        sg_chunks.append(current_chunk)
                        current_chunk = [sg]
                    else:
                        # Edge case: single SG exceeds limit (shouldn't happen with normal SG IDs)
                        logger.warning(f"Security group ID {sg} is unusually long")
                        sg_chunks.append([sg])
                else:
                    # Add SG to current chunk
                    current_chunk = test_chunk

            # Add the last chunk if it has any items
            if current_chunk:
                sg_chunks.append(current_chunk)

            # Update total chunks count and create tags
            for chunk_idx, sg_chunk in enumerate(sg_chunks):

                sg_backup = {
                    "n": {  # NetworkInterface shortened to n
                        "i": interface_id,  # ni shortened to i
                        "s": sg_chunk,  # sg shortened to s, with 'sg-' prefix stripped
                        "c": {              # ci shortened to c
                            "i": chunk_idx,
                            "t": len(sg_chunks)
                        }
                    },
                    "t": timestamp  # ts shortened to t
                }
                tag_value = json.dumps(sg_backup)
                tag_key = f"Original_SG_Backup_{instance_id}_{timestamp}_{idx}_{chunk_idx}"

                # Create the tag
                ec2_client.create_tags(
                    Resources=[instance_id],
                    Tags=[{"Key": tag_key, "Value": tag_value}],
                )

                # Verify the tag was created
                response = ec2_client.describe_tags(
                    Filters=[
                        {"Name": "resource-id", "Values": [instance_id]},
                        {"Name": "key", "Values": [tag_key]}
                    ]
                )

                if not response["Tags"]:
                    logger.error(f"Failed to verify creation of backup tag '{tag_key}' for instance {instance_id}")
                    raise Exception("Backup tag creation could not be verified")

                created_tag_value = response["Tags"][0]["Value"]
                if created_tag_value != tag_value:
                    logger.error(f"Created tag value does not match expected value for instance {instance_id}")
                    raise Exception("Backup tag value mismatch")

                logger.info(f"Backup tag '{tag_key}' chunk {chunk_idx + 1}/{len(sg_chunks)} created and verified for interface {interface_id}.")
    except ClientError as e:
        logger.error(f"AWS API error while creating/verifying backup tag: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error while creating/verifying backup tag: {str(e)}")
        raise


def modify_security_groups(ec2_client, instance_id, sg_list, timestamp, mode="remove", options=None):
    """
    Modifies security groups on network interfaces based on the specified mode.
    In 'remove' mode: Removes all SGs in sg_list from each interface
    In 'keep_only' mode: Keeps only the SGs in sg_list and removes all others

    Args:
        ec2_client: The boto3 EC2 client
        instance_id: The ID of the EC2 instance
        sg_list: List of security group IDs to remove or keep
        timestamp: Unix timestamp for backup tag
        mode: Either "remove" or "keep_only" to determine operation mode

    Raises:
        ClientError: If AWS API calls fail
        Exception: For other unexpected errors
    """
    try:
        # Get instance details
        state, _, interfaces = get_instance_details(ec2_client, instance_id)

        # Create a backup tag before making any changes
        try:
            create_backup_tag(ec2_client, instance_id, interfaces, timestamp)
            try:
                set_lastfence_tag(ec2_client, instance_id, timestamp)
            except Exception as e:
                if "--ignore-tag-write-failure" in options:
                    logger.warning(f"Failed to set lastfence tag but continuing due to --ignore-tag-write-failure: {str(e)}")
                    logger.info("Will rely on security group state for fencing status")
                else:
                    logger.error(f"Failed to set lastfence tag: {str(e)}")
                    raise
        except Exception as e:
            if "--ignore-tag-write-failure" in options:
                logger.warning(f"Failed to create backup tag but continuing due to --ignore-tag-write-failure: {str(e)}")
                logger.info("Will rely on security group state for fencing status")
            else:
                logger.error(f"Failed to create backup tag: {str(e)}")
                raise

        changed_any = False
        for interface in interfaces:
            try:
                original_sgs = interface["SecurityGroups"]

                if mode == "remove":
                    # Exclude any SGs that are in sg_list
                    updated_sgs = [sg for sg in original_sgs if sg not in sg_list]
                    operation_desc = f"removing {sg_list}"
                else:  # keep_only mode
                    # Set interface to only use the specified security groups
                    updated_sgs = sg_list
                    operation_desc = f"keeping only {sg_list}"

                # Skip if we'd end up with zero SGs (only in remove mode)
                if mode == "remove" and not updated_sgs:
                    logger.info(
                        f"Skipping interface {interface['NetworkInterfaceId']}: "
                        f"removal of {sg_list} would leave 0 SGs."
                    )
                    continue

                # Skip if no changes needed
                if updated_sgs == original_sgs:
                    continue

                logger.info(
                    f"Updating interface {interface['NetworkInterfaceId']} from {original_sgs} "
                    f"to {updated_sgs} ({operation_desc})"
                )

                try:
                    ec2_client.modify_network_interface_attribute(
                        NetworkInterfaceId=interface["NetworkInterfaceId"],
                        Groups=updated_sgs
                    )
                    changed_any = True
                except ClientError as e:
                    logger.error(
                        f"Failed to modify security groups for interface "
                        f"{interface['NetworkInterfaceId']}: {str(e)}"
                    )
                    continue

            except KeyError as e:
                logger.error(f"Malformed interface data: {str(e)}")
                continue

        # If we didn't modify anything, raise an error
        if not changed_any:
            if mode == "remove":
                error_msg = f"Security Groups {sg_list} not removed from any interface. Either not found, or removal left 0 SGs."
            else:
                error_msg = f"Security Groups {sg_list} not found on any interface. No changes made."
            logger.error(error_msg)
            raise Exception("Failed to modify security groups: " + error_msg)

        # Wait a bit for changes to propagate
        time.sleep(5)

    except ClientError as e:
        logger.error(f"AWS API error: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise

def restore_security_groups(ec2_client, instance_id):
    """
    Restores the original security groups from backup tags to each network interface.
    Each network interface's original security groups are stored in a separate backup tag.
    All backup tags share the same timestamp as the lastfence tag for validation.

    The process:
    1. Get lastfence tag timestamp
    2. Find all backup tags with matching timestamp
    3. Create a map of interface IDs to their original security groups
    4. Restore each interface's security groups from the map
    5. Clean up matching backup tags and lastfence tag

    Args:
        ec2_client: The boto3 EC2 client
        instance_id: The ID of the EC2 instance

    Raises:
        ClientError: If AWS API calls fail
        Exception: For other unexpected errors
        SystemExit: If required tags are missing or no changes were made
    """
    try:
        # Get the lastfence tag first
        lastfence_response = ec2_client.describe_tags(
            Filters=[
                {"Name": "resource-id", "Values": [instance_id]},
                {"Name": "key", "Values": ["lastfence"]}
            ]
        )

        if not lastfence_response["Tags"]:
            logger.error(f"No lastfence tag found for instance {instance_id}")
            sys.exit(EC_GENERIC_ERROR)

        lastfence_timestamp = lastfence_response["Tags"][0]["Value"]

        # Get all backup tags for this instance
        backup_response = ec2_client.describe_tags(
            Filters=[
                {"Name": "resource-id", "Values": [instance_id]},
                {"Name": "key", "Values": [f"Original_SG_Backup_{instance_id}*"]}
            ]
        )

        if not backup_response["Tags"]:
            logger.error(f"No backup tags found for instance {instance_id}")
            sys.exit(EC_GENERIC_ERROR)

        # Find and combine backup tags with matching timestamp
        matching_backups = {}
        interface_chunks = {}

        for tag in backup_response["Tags"]:
            try:
                backup_data = json.loads(tag["Value"])
                backup_timestamp = backup_data.get("t")  # Using shortened timestamp field

                if not backup_timestamp or str(backup_timestamp) != str(lastfence_timestamp):
                    continue

                logger.info(f"Found matching backup tag {tag['Key']}")
                interface_data = backup_data.get("n")  # Using shortened NetworkInterface field

                if not interface_data or "i" not in interface_data:  # Using shortened interface id field
                    continue

                interface_id = interface_data["i"]  # Using shortened interface id field
                chunk_info = interface_data.get("c", {})  # Using shortened chunk info field
                chunk_index = chunk_info.get("i", 0)
                total_chunks = chunk_info.get("t", 1)

                # Initialize tracking for this interface if needed
                if interface_id not in interface_chunks:
                    interface_chunks[interface_id] = {
                        "total": total_chunks,
                        "chunks": {},
                        "security_groups": []
                    }

                # Add this chunk's security groups
                interface_chunks[interface_id]["chunks"][chunk_index] = interface_data["s"]  # Using shortened security groups field

                # If we have all chunks for this interface, combine them
                if len(interface_chunks[interface_id]["chunks"]) == total_chunks:
                    # Combine chunks and restore 'sg-' prefix
                    combined_sgs = []
                    for i in range(total_chunks):
                        chunk_sgs = interface_chunks[interface_id]["chunks"][i]
                        # Add back 'sg-' prefix if not already present
                        restored_sgs = ['sg-' + sg if not sg.startswith('sg-') else sg for sg in chunk_sgs]
                        combined_sgs.extend(restored_sgs)
                    matching_backups[interface_id] = combined_sgs

            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"Failed to parse backup data for tag {tag['Key']}: {str(e)}")
                continue

        if not matching_backups:
            logger.error("No complete backup data found with matching timestamp")
            sys.exit(EC_GENERIC_ERROR)

        # Get current interfaces
        _, _, current_interfaces = get_instance_details(ec2_client, instance_id)

        # Use the combined matching_backups as our backup_sg_map
        backup_sg_map = matching_backups

        changed_any = False
        for interface in current_interfaces:
            try:
                interface_id = interface["NetworkInterfaceId"]
                if interface_id not in backup_sg_map:
                    logger.warning(
                        f"No backup data found for interface {interface_id}. Skipping."
                    )
                    continue

                original_sgs = backup_sg_map[interface_id]
                current_sgs = interface["SecurityGroups"]

                if original_sgs == current_sgs:
                    logger.info(
                        f"Interface {interface_id} already has original security groups. Skipping."
                    )
                    continue

                logger.info(
                    f"Restoring interface {interface_id} from {current_sgs} "
                    f"to original security groups {original_sgs}"
                )

                try:
                    ec2_client.modify_network_interface_attribute(
                        NetworkInterfaceId=interface_id,
                        Groups=original_sgs
                    )
                    changed_any = True
                except ClientError as e:
                    logger.error(
                        f"Failed to restore security groups for interface "
                        f"{interface_id}: {str(e)}"
                    )
                    continue

            except KeyError as e:
                logger.error(f"Malformed interface data: {str(e)}")
                continue

        if not changed_any:
            logger.error("No security groups were restored. All interfaces skipped.")
            sys.exit(EC_GENERIC_ERROR)

        # Wait for changes to propagate
        time.sleep(5)

        # Clean up only the matching backup tags and lastfence tag after successful restore
        try:
            # Delete all backup tags that match the lastfence timestamp
            tags_to_delete = [{"Key": "lastfence"}]
            deleted_tag_keys = []
            for tag in backup_response["Tags"]:
                try:
                    backup_data = json.loads(tag["Value"])
                    if str(backup_data.get("t")) == str(lastfence_timestamp):  # Using shortened timestamp field
                        tags_to_delete.append({"Key": tag["Key"]})
                        deleted_tag_keys.append(tag["Key"])
                except (json.JSONDecodeError, KeyError):
                    continue

            if len(tags_to_delete) > 1:  # More than just the lastfence tag
                ec2_client.delete_tags(
                    Resources=[instance_id],
                    Tags=tags_to_delete
                )
                logger.info(f"Removed matching backup tags {deleted_tag_keys} and lastfence tag from instance {instance_id}")
        except ClientError as e:
            logger.warning(f"Failed to remove tags: {str(e)}")
            # Continue since the restore operation was successful

    except ClientError as e:
        logger.error(f"AWS API error: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise

# Shutdown instance
def shutdown_instance(ec2_client, instance_id):
    """Shutdown the instance and confirm the state transition."""
    try:
        logger.info(f"Initiating shutdown for instance {instance_id}...")
        ec2_client.stop_instances(InstanceIds=[instance_id], Force=True)

        while True:
            try:
                state, _, _ = get_instance_details(ec2_client, instance_id)
                logger.info(f"Current instance state: {state}")
                if state == "stopping":
                    logger.info(
                        f"Instance {instance_id} is transitioning to 'stopping'. Proceeding without waiting further."
                    )
                    break
            except ClientError as e:
                logger.error(f"Failed to get instance state during shutdown: {str(e)}")
                fail_usage(f"AWS API error while checking instance state: {str(e)}")
            except Exception as e:
                logger.error(f"Unexpected error while checking instance state: {str(e)}")
                fail_usage(f"Failed to check instance state: {str(e)}")

    except ClientError as e:
        logger.error(f"AWS API error during instance shutdown: {str(e)}")
        fail_usage(f"Failed to shutdown instance: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error during instance shutdown: {str(e)}")
        fail_usage(f"Failed to shutdown instance due to unexpected error: {str(e)}")


# Perform the fencing action
def get_nodes_list(conn, options):
    """Get list of nodes and their status."""
    logger.debug("Starting monitor operation")
    result = {}
    try:
        if "--filter" in options:
            filter_key = options["--filter"].split("=")[0].strip()
            filter_value = options["--filter"].split("=")[1].strip()
            filter = [{"Name": filter_key, "Values": [filter_value]}]
            logging.debug("Filter: {}".format(filter))

        for instance in conn.instances.filter(Filters=filter if 'filter' in vars() else []):
            instance_name = ""
            for tag in instance.tags or []:
                if tag.get("Key") == "Name":
                    instance_name = tag["Value"]
            try:
                result[instance.id] = (instance_name, status[instance.state["Name"]])
            except KeyError as e:
                if options.get("--original-action") == "list-status":
                    logger.error("Unknown status \"{}\" returned for {} ({})".format(instance.state["Name"], instance.id, instance_name))
                result[instance.id] = (instance_name, "unknown")
    except Exception as e:
        logger.error("Failed to get node list: %s", e)
    return result

def set_lastfence_tag(ec2_client, instance_id, timestamp):
    """Set a lastfence tag on the instance with the timestamp."""
    try:
        ec2_client.create_tags(
            Resources=[instance_id],
            Tags=[{"Key": "lastfence", "Value": str(timestamp)}]
        )
        logger.info(f"Set lastfence tag with timestamp {timestamp} on instance {instance_id}")
    except Exception as e:
        logger.error(f"Failed to set lastfence tag: {str(e)}")
        raise

def set_power_status(conn, options):
    """Set power status of the instance."""
    timestamp = int(time.time())  # Unix timestamp
    ec2_client = conn.meta.client
    instance_id = options["--plug"]
    sg_to_remove = options.get("--secg", "").split(",") if options.get("--secg") else []

    # Perform self-check if skip-race not set
    if "--skip-race-check" not in options:
        self_instance_id = get_instance_id()
        if self_instance_id == instance_id:
            fail_usage("Self-fencing detected. Exiting.")

    try:
        # Only verify instance is running for 'off' action
        if options["--action"] == "off":
            instance_state, _, _ = get_instance_details(ec2_client, instance_id)
            if instance_state != "running":
                fail_usage(f"Instance {instance_id} is not running. Exiting.")

        if options["--action"] == "on":
            if not "--unfence-ignore-restore" in options:
                restore_security_groups(ec2_client, instance_id)
            else:
                logger.info("Ignored Restoring security groups as --unfence-ignore-restore is set")
        elif options["--action"] == "off":
            if sg_to_remove:
                mode = "keep_only" if "--invert-sg-removal" in options else "remove"
                try:
                    modify_security_groups(ec2_client, instance_id, sg_to_remove, timestamp, mode, options)
                    if "--onfence-poweroff" in options:
                        shutdown_instance(ec2_client, instance_id)
                except Exception as e:
                    if isinstance(e, ClientError):
                        logger.error("AWS API error: %s", e)
                        fail_usage(str(e))
                    elif "--ignore-tag-write-failure" in options:
                        # If we're ignoring tag failures, only fail if the security group modifications failed
                        if "Failed to modify security groups" in str(e):
                            logger.error("Failed to modify security groups: %s", e)
                            fail(EC_STATUS)
                        else:
                            logger.warning("Ignoring error due to ignore-tag-write-failure: %s", e)
                    else:
                        logger.error("Failed to set power status: %s", e)
                        fail(EC_STATUS)
    except Exception as e:
        logger.error("Unexpected error in set_power_status: %s", e)
        fail(EC_STATUS)


# Define fencing agent options
def define_new_opts():
    all_opt["port"]["help"] = "-n, --plug=[id]                AWS Instance ID to perform action on "
    all_opt["port"]["shortdesc"] = "AWS Instance ID to perform action on "

    all_opt["region"] = {
        "getopt": "r:",
        "longopt": "region",
        "help": "-r, --region=[region]          AWS region (e.g., us-east-1)",
        "shortdesc": "AWS Region.",
        "required": "0",
        "order": 1,
    }
    all_opt["access_key"] = {
        "getopt": "a:",
        "longopt": "access-key",
        "help": "-a, --access-key=[key]         AWS access key.",
        "shortdesc": "AWS Access Key.",
        "required": "0",
        "order": 2,
    }
    all_opt["secret_key"] = {
        "getopt": "s:",
        "longopt": "secret-key",
        "help": "-s, --secret-key=[key]         AWS secret key.",
        "shortdesc": "AWS Secret Key.",
        "required": "0",
        "order": 3,
    }
    all_opt["secg"] = {
        "getopt": ":",
        "longopt": "secg",
        "help": "--secg=[sg1,sg2,...]           Comma-separated list of Security Groups to remove.",
        "shortdesc": "Security Groups to remove.",
        "required": "0",
        "order": 4,
    }
    all_opt["skip_race_check"] = {
        "getopt": "",
        "longopt": "skip-race-check",
        "help": "--skip-race-check              Skip race condition check.",
        "shortdesc": "Skip race condition check.",
        "required": "0",
        "order": 6,
    }
    all_opt["invert-sg-removal"] = {
        "getopt": "",
        "longopt": "invert-sg-removal",
        "help": "--invert-sg-removal            Remove all security groups except the specified one(s).",
        "shortdesc": "Remove all security groups except specified..",
        "required": "0",
        "order": 7,
    }
    all_opt["unfence-ignore-restore"] = {
        "getopt": "",
        "longopt": "unfence-ignore-restore",
        "help": "--unfence-ignore-restore       Do not restore security groups from tag when unfencing (off).",
        "shortdesc": "Remove all security groups except specified..",
        "required": "0",
        "order": 8,

    }
    all_opt["filter"] = {
        "getopt": ":",
        "longopt": "filter",
        "help": "--filter=[key=value]           Filter (e.g. vpc-id=[vpc-XXYYZZAA])",
        "shortdesc": "Filter for list-action",
        "required": "0",
        "order": 9
    }
    all_opt["boto3_debug"] = {
        "getopt": "b:",
        "longopt": "boto3_debug",
        "help": "-b, --boto3_debug=[option]     Boto3 and Botocore library debug logging",
        "shortdesc": "Boto Lib debug",
        "required": "0",
        "default": "False",
        "order": 10
    }
    all_opt["onfence-poweroff"] = {
        "getopt": "",
        "longopt": "onfence-poweroff",
        "help": "--onfence-poweroff             Power off the machine async upon fence (this is a network fencing agent...)",
        "shortdesc": "Power off the machine async..",
        "required": "0",
        "order": 11
    }
    all_opt["ignore-tag-write-failure"] = {
        "getopt": "",
        "longopt": "ignore-tag-write-failure",
        "help": "--ignore-tag-write-failure     Continue to fence even if backup tag fails.  This ensures prioriization of fencing over AWS backplane access",
        "shortdesc": "Continue to fence even if backup tag fails..",
        "required": "0",
        "order": 12
    }


def main():
    conn = None

    device_opt = [
        "no_password",
        "region",
        "access_key",
        "secret_key",
        "secg",
        "port",
        "skip_race_check",
        "invert-sg-removal",
        "unfence-ignore-restore",
        "filter",
        "boto3_debug",
        "onfence-poweroff",
        "ignore-tag-write-failure"
]

    atexit.register(atexit_handler)

    define_new_opts()

    try:
        processed_input = process_input(device_opt)
        options = check_input(device_opt, processed_input)
    except Exception as e:
        logger.error(f"Failed to process input options: {str(e)}")
        sys.exit(EC_GENERIC_ERROR)

    run_delay(options)

    docs = {
        "shortdesc": "Fence agent for AWS (Amazon Web Services) Net",
        "longdesc": (
            "fence_aws_vpc is a Network and Power Fencing agent for AWS VPC that works by "
            "manipulating security groups. It uses the boto3 library to connect to AWS.\n\n"
            "boto3 can be configured with AWS CLI or by creating ~/.aws/credentials.\n"
            "For instructions see: https://boto3.readthedocs.io/en/latest/guide/quickstart.html#configuration"
            " "
            "NOTE: If onfence-poweroff is set, the agent won't be able to power on the node again, it will have to be powered on manually or with other automation."
        ),
        "vendorurl": "http://www.amazon.com"
    }
    show_docs(options, docs)

    if "--onfence-poweroff" not in options and options.get("--action", "") == "reboot":
        options["--action"] = "off"

    # Configure logging
    if "--debug-file" in options:
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler):
                logger.removeHandler(handler)
        lh = logging.FileHandler(options["--debug-file"])
        logger.addHandler(lh)
        lhf = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        lh.setFormatter(lhf)
        lh.setLevel(logging.DEBUG)

    # Configure boto3 logging
    if options.get("--boto3_debug", "").lower() not in ["1", "yes", "on", "true"]:
        boto3.set_stream_logger('boto3', logging.INFO)
        boto3.set_stream_logger('botocore', logging.CRITICAL)
        logging.getLogger('botocore').propagate = False
        logging.getLogger('boto3').propagate = False
    else:
        log_format = logging.Formatter('%(asctime)s %(name)-12s %(levelname)-8s %(message)s')
        logging.getLogger('botocore').propagate = False
        logging.getLogger('boto3').propagate = False
        fdh = logging.FileHandler('/var/log/fence_aws_vpc_boto3.log')
        fdh.setFormatter(log_format)
        logging.getLogger('boto3').addHandler(fdh)
        logging.getLogger('botocore').addHandler(fdh)
        logging.debug("Boto debug level is %s and sending debug info to /var/log/fence_aws_vpc_boto3.log",
                     options.get("--boto3_debug"))

    # Establish AWS connection
    region = options.get("--region")
    access_key = options.get("--access-key")
    secret_key = options.get("--secret-key")

    try:
        conn = boto3.resource(
            "ec2",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
    except Exception as e:
        if not options.get("--action", "") in ["metadata", "manpage", "validate-all"]:
            fail_usage("Failed: Unable to connect to AWS: " + str(e))
        else:
            pass

    # Operate the fencing device using the fence library's fence_action
    result = fence_action(conn, options, set_power_status, get_power_status, get_nodes_list)
    sys.exit(result)


if __name__ == "__main__":
    main()

