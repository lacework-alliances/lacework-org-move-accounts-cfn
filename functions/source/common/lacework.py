import logging
import os

import boto3
import json

import requests

from util import error_exception

LOGLEVEL = os.environ.get('LOGLEVEL', logging.INFO)
logger = logging.getLogger()
logger.setLevel(LOGLEVEL)


def get_account_from_url(lacework_url):
    return lacework_url.split('.')[0]


def setup_initial_access_token(lacework_url, access_key_id, secret_key):
    logger.info("lacework.setup_initial_access_token called.")

    access_token_response = send_lacework_api_access_token_request(lacework_url, access_key_id, secret_key)
    logger.info('API response code : {}'.format(access_token_response.status_code))
    logger.debug('API response : {}'.format(access_token_response.text))
    if access_token_response.status_code == 201:
        payload_response = access_token_response.json()
        token = payload_response['token']
        return token
    else:
        raise error_exception("Generate access key failure {} {}".format(access_token_response.status_code,
                                                                         access_token_response.text))


def setup_initial_access_token_with_secrets_manager(lacework_url, lacework_api_credentials):
    logger.info("lacework.setup_initial_access_token_with_secrets_manager called.")
    secret_client = boto3.client('secretsmanager')
    secret_response = secret_client.get_secret_value(
        SecretId=lacework_api_credentials
    )
    if 'SecretString' not in secret_response:
        raise error_exception("SecretString not found in {}".format(lacework_api_credentials))

    secret_string_dict = json.loads(secret_response['SecretString'])
    access_key_id = secret_string_dict['AccessKeyID']
    secret_key = secret_string_dict['SecretKey']

    access_token_response = send_lacework_api_access_token_request(lacework_url, access_key_id, secret_key)
    logger.info('API response code : {}'.format(access_token_response.status_code))
    logger.debug('API response : {}'.format(access_token_response.text))
    if access_token_response.status_code == 201:
        payload_response = access_token_response.json()
        expires_at = payload_response['expiresAt']
        token = payload_response['token']
        secret_string_dict['AccessToken'] = token
        secret_string_dict['TokenExpiry'] = expires_at
        secret_client.update_secret(SecretId=lacework_api_credentials, SecretString=json.dumps(secret_string_dict))
        logger.info("New access token saved to secrets manager.")
        return token
    else:
        raise error_exception("Generate access key failure {} {}".format(access_token_response.status_code,
                                                                         access_token_response.text))


def get_access_token_from_secrets_manager(lacework_api_credentials):
    logger.info("lacework.get_access_token_from_secrets_manager called.")

    secret_client = boto3.client('secretsmanager')
    secret_response = secret_client.get_secret_value(
        SecretId=lacework_api_credentials
    )
    if 'SecretString' not in secret_response:
        raise error_exception("SecretString not found in {}".format(lacework_api_credentials))

    secret_string_dict = json.loads(secret_response['SecretString'])
    access_token = secret_string_dict['AccessToken']

    return access_token


def lw_cloud_account_exists_in_orgs(integration_name, lacework_url, access_token, orgs):
    logger.info("lacework.lw_cloud_account_exists_in_orgs")
    org_list = [x.strip() for x in orgs.split(',')]
    for org in org_list:
        data_dict = search_lw_cloud_account_by_name(integration_name, lacework_url, org, access_token)
        if data_dict:
            return True

    return False


def delete_lw_cloud_account_by_int_guid(intg_guid, lacework_url, access_token, sub_account):
    logger.info("lacework.delete_lw_cloud_account_by_int_guid")
    delete_response = send_lacework_api_delete_request(lacework_url, "api/v2/CloudAccounts/"
                                                       + intg_guid, access_token, sub_account)
    logger.info('API response code : {}'.format(delete_response.status_code))
    logger.info('API response : {}'.format(delete_response.text))
    if delete_response.status_code == 204:
        return True
    else:
        logger.warning(
            "API response error deleting Config account {} {}".format(delete_response.status_code,
                                                                      delete_response.text))
        return False


def delete_lw_cloud_account_in_orgs(integration_name, lacework_url, access_token, orgs):
    logger.info("lacework.lw_cloud_account_exists_in_orgs")
    org_list = [x.strip() for x in orgs.split(',')]
    for org in org_list:
        data_dict = search_lw_cloud_account_by_name(integration_name, lacework_url, org, access_token)
        if data_dict:
            return delete_lw_cloud_account_by_int_guid(data_dict['intgGuid'], lacework_url, access_token, org)
    logger.warning("integration name {} not found for deletion.")
    return False


def update_lw_cloud_account_in_orgs(integration_name, lacework_url, sub_account_name,
                                    access_token,
                                    orgs, role_arn, acct):
    logger.info("lacework.update_lw_cloud_account_in_orgs")
    org_list = [x.strip() for x in orgs.split(',')]
    for org in org_list:
        data_dict = search_lw_cloud_account_by_name(integration_name, lacework_url, org, access_token)
        if data_dict:
            delete_lw_cloud_account_by_int_guid(data_dict['intgGuid'], lacework_url, access_token, org)
            add_lw_cloud_account_for_cfg(integration_name, lacework_url, sub_account_name,
                                         access_token,
                                         data_dict['data']['crossAccountCredentials']['externalId'],
                                         role_arn, acct)
            logger.info(
                "Updated acct {} to {} in Lacework. Moved to {}".format(acct, integration_name, sub_account_name))
            return True
        else:
            logger.info("integration name {} not found in org {}".format(integration_name, org))
    logger.warning("integration name {} not found for update.")
    return False


def lw_cloud_account_exists(integration_name, lacework_url, access_token, sub_account=""):
    logger.info("lacework.lw_cloud_account_exists")
    data_dict = search_lw_cloud_account_by_name(integration_name, lacework_url, sub_account, access_token)

    if data_dict:
        return True
    else:
        return False


def add_lw_cloud_account_for_ct(integration_name, lacework_url, sub_account, access_token,
                                external_id,
                                role_arn, sqs_queue_url):
    logger.info("lacework.add_lw_cloud_account_for_ct")

    request_payload = '''
    {{
        "name": "{}", 
        "type": "AwsCtSqs",
        "enabled": 1,
        "data": {{
            "crossAccountCredentials": {{
                "externalId": "{}",
                "roleArn": "{}"
            }},
            "queueUrl": "{}"
        }}
    }}
    '''.format(integration_name, external_id, role_arn, sqs_queue_url)
    logger.info('Generate create account payload : {}'.format(request_payload))

    add_response = send_lacework_api_post_request(lacework_url, "api/v2/CloudAccounts", access_token,
                                                  request_payload, sub_account)
    logger.info('API response code : {}'.format(add_response.status_code))
    logger.info('API response : {}'.format(add_response.text))
    if add_response.status_code == 201:
        return True
    else:
        logger.warning("API response error adding CloudTrail account {} {}".format(add_response.status_code,
                                                                                   add_response.text))
        return False


def add_lw_cloud_account_for_cfg(integration_name, lacework_url, account_name, access_token,
                                 external_id,
                                 role_arn, aws_account_id):
    logger.info("lacework.add_lw_cloud_account_for_cfg")

    request_payload = '''
    {{
        "name": "{}", 
        "type": "AwsCfg",
        "enabled": 1,
        "data": {{
            "crossAccountCredentials": {{
                "externalId": "{}",
                "roleArn": "{}"
            }},
            "awsAccountId": "{}"
        }}
    }}
    '''.format(integration_name, external_id, role_arn, aws_account_id)
    logger.info('Generate create account payload : {}'.format(request_payload))

    add_response = send_lacework_api_post_request(lacework_url, "api/v2/CloudAccounts", access_token,
                                                  request_payload, account_name)
    logger.info('API response code : {}'.format(add_response.status_code))
    logger.info('API response : {}'.format(add_response.text))
    if add_response.status_code == 201:
        return True
    else:
        logger.warning("API response error adding Config account {} {}".format(add_response.status_code,
                                                                               add_response.text))
        return False


def delete_lw_cloud_account(integration_name, lacework_url, sub_account, access_token):
    logger.info("lacework.delete_lw_cloud_account")

    data_dict = search_lw_cloud_account_by_name(integration_name, lacework_url, sub_account, access_token)

    if data_dict:
        return delete_lw_cloud_account_by_int_guid(data_dict['intgGuid'], lacework_url, access_token, sub_account)
    else:
        logger.warning("Cloud account {} not deleted. int_guid not found.".format(integration_name))
        return False


def move_bulk_lw_cloud_accounts(lacework_url, accounts, from_sub_account, to_sub_account, access_token):
    logger.info("lacework.move_lw_cloud_accounts")

    data_dict = get_lw_cloud_accounts(lacework_url, from_sub_account, access_token)
    account_list = [item.strip() for item in accounts.split(",")]
    logger.info("Processing accounts {}.".format(account_list if accounts else "All"))

    if data_dict:
        for account_dict in data_dict:
            account_data_dict = account_dict["data"]
            cross_account_creds_dict = account_data_dict["crossAccountCredentials"]
            role_arn = cross_account_creds_dict["roleArn"]
            aws_account_id = role_arn.split(":")[4]
            if accounts and aws_account_id not in account_list:
                logger.info("Skipping account {}.".format(aws_account_id))
                continue

            integration_name = account_dict["name"]
            intg_guid = account_dict["intgGuid"]
            account_type = account_dict["type"]
            external_id = cross_account_creds_dict["externalId"]

            logger.info("Moving account {} with name {} of type {}.".format(aws_account_id, intg_guid, account_type))

            if delete_lw_cloud_account_by_int_guid(intg_guid, lacework_url, access_token, from_sub_account):
                logger.info("Deleted account {} {} from sub-account {}.".format(aws_account_id, intg_guid,
                                                                                from_sub_account))
                if account_type == "AwsCfg":
                    if add_lw_cloud_account_for_cfg(integration_name, lacework_url, to_sub_account,
                                                    access_token, external_id, role_arn, aws_account_id):
                        logger.info("Added AwsCfg account {} {} to sub-account {}.".format(aws_account_id, intg_guid,
                                    to_sub_account))
                    else:
                        logger.warning("Failed to add account {} {} to sub-account {}.".format(aws_account_id, intg_guid,
                                       to_sub_account))
                elif account_type == "AwsCtSqs":
                    queue_url = account_data_dict["queueUrl"]
                    if add_lw_cloud_account_for_ct(integration_name, lacework_url, to_sub_account,
                                                   access_token, external_id, role_arn, queue_url):
                        logger.info("Added AwsCtSqs account {} {} to sub-account {}.".format(aws_account_id,
                                                                                             intg_guid, to_sub_account))
                    else:
                        logger.warning("Failed to add account {} {} to sub-account {}.".format(aws_account_id, intg_guid,
                                       to_sub_account))
                else:
                    logger.warning("Unknown account type found {}.".format(account_type))
            else:
                logger.warning("Failed to delete account {} {} from sub-account {}.".format(aws_account_id, intg_guid,
                               from_sub_account))
        return True
    else:
        logger.warning("No accounts found in sub-account {}.".format(from_sub_account))
        return False


def search_lw_cloud_account_by_name(integration_name, lacework_url, sub_account, access_token):
    logger.info("lacework.search_lw_cloud_account_by_name: {}".format(integration_name))

    search_request_payload = '''
    {{
        "filters": [
            {{
                "field": "name",
                "expression": "eq",
                "value": "{}"
            }}
        ],
        "returns": [
            "intgGuid"
        ]
    }}
    '''.format(integration_name)

    search_response = send_lacework_api_post_request(lacework_url, "api/v2/CloudAccounts/search", access_token,
                                                     search_request_payload, sub_account)
    logger.info('API response code : {}'.format(search_response.status_code))
    logger.info('API response : {}'.format(search_response.text))
    if search_response.status_code == 200:
        search_response_dict = json.loads(search_response.text)
        data_dict = search_response_dict['data'];
        if len(data_dict) == 0:
            logger.warning("Cloud account with integration name {} was not found.".format(integration_name))
            return False
        elif len(data_dict) > 1:
            logger.warning(
                "More than one cloud account with integration name {} was found.".format(integration_name))
            return False
        return data_dict[0]
    else:
        return False


def get_lw_cloud_accounts(lacework_url, sub_account, access_token):
    logger.info("lacework.get_lw_cloud_accounts: {}".format(sub_account))

    response = send_lacework_api_get_request(lacework_url, "api/v2/CloudAccounts", access_token, sub_account)
    logger.info('API response code : {}'.format(response.status_code))
    logger.info('API response : {}'.format(response.text))
    if response.status_code == 200:
        response_dict = json.loads(response.text)
        data_dict = response_dict['data'];
        if len(data_dict) == 0:
            logger.warning("Cloud accounts were not found.")
            return False
        return data_dict
    else:
        return False


def send_lacework_api_access_token_request(lacework_url, access_key_id, secret_key):
    logger.info("lacework.send_lacework_api_access_token_request: {}".format(lacework_url))
    request_payload = '''
        {{
            "keyId": "{}", 
            "expiryTime": 86400
        }}
        '''.format(access_key_id)
    logger.debug('Generate access key payload : {}'.format(json.dumps(request_payload)))
    try:
        return requests.post("https://" + lacework_url + "/api/v2/access/tokens",
                             headers={'X-LW-UAKS': secret_key, 'content-type': 'application/json'},
                             verify=True, data=request_payload)
    except Exception as api_request_exception:
        raise api_request_exception


def send_lacework_api_get_request(lacework_url, api, access_token, account_name):
    logger.info("lacework.send_lacework_api_get_request: {} {} {}".format(lacework_url, api, account_name))
    try:
        if not account_name:
            return requests.get("https://" + lacework_url + "/" + api,
                                headers={'Authorization': access_token, 'content-type': 'application/json'},
                                verify=True)
        else:
            return requests.get("https://" + lacework_url + "/" + api,
                                headers={'Authorization': access_token, 'content-type': 'application/json',
                                         'Account-Name': account_name.lower()},
                                verify=True)
    except Exception as api_request_exception:
        raise api_request_exception


def send_lacework_api_post_request(lacework_url, api, access_token, request_payload, account_name):
    logger.info("lacework.send_lacework_api_post_request: {} {} {} {}".format(lacework_url, api, account_name,
                                                                              request_payload))
    try:
        if not account_name:
            return requests.post("https://" + lacework_url + "/" + api,
                                 headers={'Authorization': access_token, 'content-type': 'application/json'},
                                 verify=True, data=request_payload)
        else:
            return requests.post("https://" + lacework_url + "/" + api,
                                 headers={'Authorization': access_token, 'content-type': 'application/json',
                                          'Account-Name': account_name.lower()},
                                 verify=True, data=request_payload)
    except Exception as api_request_exception:
        raise api_request_exception


def send_lacework_api_delete_request(lacework_url, api, access_token, account_name):
    logger.info("lacework.send_lacework_api_delete_request: {} {} {}".format(lacework_url, api, account_name))
    try:
        if not account_name:
            return requests.delete("https://" + lacework_url + "/" + api,
                                   headers={'Authorization': access_token},
                                   verify=True)
        else:
            return requests.delete("https://" + lacework_url + "/" + api,
                                   headers={'Authorization': access_token,
                                            'Account-Name': account_name.lower()},
                                   verify=True)
    except Exception as api_request_exception:
        raise api_request_exception


def get_lacework_environment_variables():
    env_vars = {}
    for key, value in os.environ.items():
        if key.startswith("lacework"):
            env_vars[key] = value

    return json.dumps(env_vars)
