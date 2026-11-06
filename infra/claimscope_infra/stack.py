from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Size,
    Stack,
)
from aws_cdk import aws_iam as iam
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_s3_notifications as s3n
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from .build import build_lambda
from .secrets import load_demo_secrets

ROOT = Path(__file__).resolve().parents[2]
ANTHROPIC_SECRET_NAME = "claimscope/anthropic-api-key"


class ClaimScopeStack(Stack):
    """Week-1 demo: static page on S3 + CloudFront, one API Lambda, one async pipeline Lambda."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        table = dynamodb.TableV2(
            self,
            "Table",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            time_to_live_attribute="ttl",  # demo rate-limit counters expire on their own
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        demo_secrets = load_demo_secrets()
        # Exempt addresses live in the git-ignored secrets file (or -c rateLimitExemptIps=...): home IPs are personal data.
        exempt_ips = self.node.try_get_context("rateLimitExemptIps") or demo_secrets.get("rate_limit_exempt_ips", "")

        # --- HTTP API (created first so CloudFront can point at it) ----------
        api = apigw.HttpApi(self, "Api", create_default_stage=False)
        apigw.HttpStage(
            self,
            "ApiStage",
            http_api=api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigw.ThrottleSettings(rate_limit=10, burst_limit=20),
        )

        # --- Web front end ----------------------------------------------------
        web_bucket = s3.Bucket(
            self,
            "WebBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        distribution = cloudfront.Distribution(
            self,
            "Web",
            default_root_object="index.html",
            # Switch the whole demo (site and API) off or on: "siteEnabled" in cdk.json. Data is kept either way.
            enabled=self.node.try_get_context("siteEnabled") not in (False, "false"),
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(web_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(
                        f"{api.api_id}.execute-api.{self.region}.amazonaws.com",
                        custom_headers={"x-origin-verify": demo_secrets["origin_verify"]},
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy(
                        self,
                        "ApiOriginRequest",
                        header_behavior=cloudfront.OriginRequestHeaderBehavior.allow_list(
                            "content-type", "CloudFront-Viewer-Address"
                        ),
                        query_string_behavior=cloudfront.OriginRequestQueryStringBehavior.all(),
                        cookie_behavior=cloudfront.OriginRequestCookieBehavior.none(),
                    ),
                ),
            },
        )
        site_origin = f"https://{distribution.distribution_domain_name}"

        # --- Evidence (photos, scan views, voice, model I/O, schedules) -------
        evidence_bucket = s3.Bucket(
            self,
            "EvidenceBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS_MANAGED,
            enforce_ssl=True,
            versioned=False,
            removal_policy=RemovalPolicy.RETAIN,
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.PUT],
                    allowed_origins=[site_origin, "http://127.0.0.1:8765", "http://localhost:8765"],  # tools/dev/serve.py
                    allowed_headers=["content-type"],
                    max_age=3000,
                )
            ],
            lifecycle_rules=[
                s3.LifecycleRule(abort_incomplete_multipart_upload_after=Duration.days(1)),
                # Demo retention for captured views (evidence thumbnails, re-analysis, eval set).
                # Live claim data needs the retention agreed in the DPA.
                s3.LifecycleRule(prefix="captures/", expiration=Duration.days(30)),
                s3.LifecycleRule(prefix="videos/", expiration=Duration.days(30)),
                s3.LifecycleRule(prefix="transcripts/", expiration=Duration.days(30)),
                s3.LifecycleRule(prefix="audio/", expiration=Duration.days(7)),  # only needed while transcribing
            ],
        )

        # --- Lambdas ------------------------------------------------------------
        code = lambda_.Code.from_asset(str(build_lambda()))
        video_code = lambda_.Code.from_asset(str(build_lambda("video")))
        common_env = {
            "TABLE_NAME": table.table_name,
            "EVIDENCE_BUCKET": evidence_bucket.bucket_name,
        }

        pipeline_fn = lambda_.Function(
            self,
            "PipelineFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="claimscope.pipeline.handler",
            code=code,
            memory_size=1024,
            timeout=Duration.minutes(15),
            # A retried run would repeat every model call; failures are recorded on the assessment instead.
            retry_attempts=0,
            environment={**common_env, "ANTHROPIC_SECRET_NAME": ANTHROPIC_SECRET_NAME, "KEEP_CAPTURES": "1"},
            log_group=self._log_group("PipelineLogs"),
        )
        view_fn = lambda_.Function(
            self,
            "ViewFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="claimscope.pipeline.view_handler",
            code=code,
            memory_size=512,
            timeout=Duration.minutes(2),
            retry_attempts=1,
            # Caps parallel Claude calls per account so a fast auto-walk can't hit rate limits.
            reserved_concurrent_executions=8,
            environment={**common_env, "ANTHROPIC_SECRET_NAME": ANTHROPIC_SECRET_NAME, "KEEP_CAPTURES": "1"},
            log_group=self._log_group("ViewLogs"),
        )
        evidence_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED, s3n.LambdaDestination(view_fn), s3.NotificationKeyFilter(prefix="captures/")
        )

        video_fn = lambda_.Function(
            self,
            "VideoFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="claimscope.pipeline.video_handler",
            code=video_code,
            memory_size=3008,  # decoding is CPU-bound; Lambda CPU scales with memory
            timeout=Duration.minutes(15),
            ephemeral_storage_size=Size.gibibytes(4),
            retry_attempts=0,
            reserved_concurrent_executions=3,
            environment={**common_env, "ANTHROPIC_SECRET_NAME": ANTHROPIC_SECRET_NAME, "KEEP_CAPTURES": "1"},
            log_group=self._log_group("VideoLogs"),
        )
        evidence_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED, s3n.LambdaDestination(video_fn), s3.NotificationKeyFilter(prefix="videos/")
        )
        video_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["transcribe:StartTranscriptionJob", "transcribe:GetTranscriptionJob", "transcribe:DeleteTranscriptionJob"],
            resources=["*"],
        ))

        api_fn = lambda_.Function(
            self,
            "ApiFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="claimscope.api.handler",
            code=code,
            memory_size=512,
            timeout=Duration.seconds(25),
            environment={
                **common_env,
                "PIPELINE_FUNCTION": pipeline_fn.function_name,
                "ORIGIN_VERIFY": demo_secrets["origin_verify"],
                "IP_HASH_SALT": demo_secrets["ip_hash_salt"],
                "RATE_LIMIT_PER_DAY": "2",
                "RATE_LIMIT_EXEMPT_IPS": exempt_ips,
            },
            log_group=self._log_group("ApiLogs"),
        )

        table.grant_read_write_data(api_fn)
        table.grant_read_write_data(pipeline_fn)
        evidence_bucket.grant_put(api_fn)
        evidence_bucket.grant_read(api_fn)
        evidence_bucket.grant_read_write(pipeline_fn)
        evidence_bucket.grant_delete(pipeline_fn)
        evidence_bucket.grant_delete(api_fn)
        evidence_bucket.grant_read(view_fn)
        evidence_bucket.grant_delete(view_fn)
        table.grant_read_write_data(view_fn)
        table.grant_read_write_data(video_fn)
        evidence_bucket.grant_read_write(video_fn)  # Transcribe reads the audio and writes the transcript as this role
        evidence_bucket.grant_delete(video_fn)
        pipeline_fn.grant_invoke(api_fn)
        anthropic_key = secretsmanager.Secret.from_secret_name_v2(self, "AnthropicKey", ANTHROPIC_SECRET_NAME)
        anthropic_key.grant_read(pipeline_fn)
        anthropic_key.grant_read(view_fn)
        anthropic_key.grant_read(video_fn)

        api.add_routes(
            path="/api/{proxy+}",
            methods=[apigw.HttpMethod.ANY],
            integration=integrations.HttpLambdaIntegration("ApiIntegration", api_fn),
        )

        # --- Publish the static site -------------------------------------------
        s3deploy.BucketDeployment(
            self,
            "DeployWeb",
            sources=[
                s3deploy.Source.asset(str(ROOT / "apps" / "web")),
                # Matterport SDK keys are public browser keys, restricted to allowed domains in Matterport's portal.
                s3deploy.Source.json_data(
                    "config.json", {"matterportSdkKey": self.node.try_get_context("matterportSdkKey") or ""}
                ),
            ],
            destination_bucket=web_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        CfnOutput(self, "SiteUrl", value=site_origin)
        CfnOutput(self, "ApiEndpoint", value=api.api_endpoint)
        CfnOutput(self, "EvidenceBucketName", value=evidence_bucket.bucket_name)
        CfnOutput(self, "TableName", value=table.table_name)

    def _log_group(self, construct_id: str) -> logs.LogGroup:
        return logs.LogGroup(
            self, construct_id, retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY
        )
