#!/usr/bin/env python3
import os

import aws_cdk as cdk

from claimscope_infra.stack import ClaimScopeStack

app = cdk.App()
ClaimScopeStack(
    app,
    "ClaimScopeDemo",
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="eu-west-2"),
)
cdk.Tags.of(app).add("project", "claimscope")
app.synth()
