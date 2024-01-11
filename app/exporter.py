#!/usr/bin/python
# -*- coding:utf-8 -*-
# Filename: exporter.py

import time
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from prometheus_client import Gauge
from azure.identity import ClientSecretCredential
from azure.mgmt.costmanagement import CostManagementClient
from azure.mgmt.costmanagement.models import QueryDefinition, QueryTimePeriod
from azure.core.exceptions import HttpResponseError
import logging


class MetricExporter:
    def __init__(self, polling_interval_seconds, group_by, targets, secrets):
        self.polling_interval_seconds = polling_interval_seconds
        self.group_by = group_by
        self.targets = targets
        self.secrets = secrets
        # we have verified that there is at least one target
        self.labels = set(targets[0].keys())
        # for now we only support exporting one type of cost (ActualCost)
        self.labels.add("ChargeType")
        if group_by["enabled"]:
            for group in group_by["groups"]:
                self.labels.add(group["label_name"])
        self.azure_daily_cost_usd = Gauge(
            "azure_daily_cost_usd", "Daily cost of an Azure account in USD", self.labels)

    def run_metrics_loop(self):
        while True:
            self.fetch()
            time.sleep(self.polling_interval_seconds)

    def init_azure_client(self, tenant_id, subscription_id):
        credentials = next((sub for sub in self.secrets[tenant_id] if sub["SubscriptionId"] == subscription_id), None)
        if credentials is None:
            raise ValueError("Credentials for subscription %s not found" % subscription_id)
    
        client = CostManagementClient(credential=ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=credentials["client_id"],
            client_secret=credentials["client_secret"]
        ))
    
        return client

    def query_azure_cost_explorer(self, azure_client, subscription, group_by, start_date, end_date):
        scope = f"/subscriptions/{subscription}"

        groups = list()
        if group_by["enabled"]:
            for group in group_by["groups"]:
                groups.append({
                    "type": group["type"],
                    "name": group["name"]
                })

        query = QueryDefinition(
            type="ActualCost",
            dataset={
                "granularity": "Daily",
                "aggregation": {
                    "totalCostUSD":
                    {
                        "name": "CostUSD",
                        "function": "Sum"
                    }
                },
                "grouping": groups
            },
            timeframe="Custom",
            time_period=QueryTimePeriod(
                from_property=datetime(
                    start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc),
                to=datetime(end_date.year, end_date.month,
                            end_date.day, tzinfo=timezone.utc)
            )
        )
        result = azure_client.query.usage(scope, query)
        return result.as_dict()

    def expose_metrics(self, azure_account, result):
        cost = float(result[0])
        labels = {"ChargeType": "ActualCost"}

    # Add Azure account details to labels
        labels.update(azure_account)

    # If group_by is enabled, process the grouped metrics
        if self.group_by["enabled"]:
            merged_minor_cost = 0
            group_key_values = dict()

            for i in range(len(self.group_by["groups"])):
                value = result[i + 2]
                group_key_values.update({self.group_by["groups"][i]["label_name"]: value})

            if self.group_by["merge_minor_cost"]["enabled"] and cost < self.group_by["merge_minor_cost"]["threshold"]:
                merged_minor_cost += cost
            else:
                labels.update(group_key_values)
                self.azure_daily_cost_usd.labels(**labels).set(cost)
                print(f"Exposing metric with labels: {labels} and cost: {cost}")  # Debug log

            # Handle merged minor cost
            if merged_minor_cost > 0:
                group_key_values = dict()
                for group in self.group_by["groups"]:
                    group_key_values.update({group["label_name"]: self.group_by["merge_minor_cost"]["tag_value"]})
                labels.update(group_key_values)
                self.azure_daily_cost_usd.labels(**labels).set(merged_minor_cost)
                print(f"Exposing merged minor cost metric with labels: {labels} and cost: {merged_minor_cost}")  # Debug log
        else:
        # Expose metrics without group_by
            self.azure_daily_cost_usd.labels(**labels).set(cost)
            print(f"Exposing metric with labels: {labels} and cost: {cost}")  # Debug log
 
    def fetch(self):
        processed_tenants = set()  # Set to keep track of processed tenants

        for azure_account in self.targets:
            tenant_id = azure_account["TenantId"]

            # Process the tenant only if it has not been processed yet
            if tenant_id not in processed_tenants:
                print(f"Querying cost data for Azure tenant {tenant_id}")

                # Process each subscription for the tenant
                for sub in self.secrets[tenant_id]:
                    subscription_id = sub["SubscriptionId"]
                    print(f"Processing subscription {subscription_id}")

                    # Update azure_account with the current subscription ID
                    current_azure_account = azure_account.copy()
                    current_azure_account["Subscription"] = subscription_id

                    # Match EnvironmentName for the subscription
                    for target_account in self.targets:
                        if target_account.get("Subscription", "") == subscription_id:
                            current_azure_account["EnvironmentName"] = target_account.get("EnvironmentName", "DefaultEnv")
                            break

                    azure_client = self.init_azure_client(tenant_id, subscription_id)

                    try:
                        end_date = datetime.today()
                        start_date = end_date - relativedelta(days=1)
                        cost_response = self.query_azure_cost_explorer(
                            azure_client, subscription_id, self.group_by, start_date, end_date)

                        # Process the response for each row
                        for result in cost_response["rows"]:
                            if result[1] != int(start_date.strftime("%Y%m%d")):
                                continue
                            self.expose_metrics(current_azure_account, result)

                    except HttpResponseError as e:
                        logging.error(f"Error querying Azure for subscription {subscription_id}: {e.reason}")
                        continue

                # Mark the tenant as processed
                processed_tenants.add(tenant_id) 
