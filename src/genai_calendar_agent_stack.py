import os
import base64
import json
import aws_cdk as core


from aws_cdk import (
    Duration,
    Stack,
    CfnParameter,
    aws_iam as iam,
    aws_logs as logs,
    aws_apigateway as apigateway,
    aws_lambda as lambda_,
    aws_bedrock as bedrock,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks
)
from constructs import Construct

from cdk_nag import(
    NagSuppressions
)

def generate_api_key_base64(length=32):
    # Generate random bytes
    random_bytes = os.urandom(length)
    # Convert to base64 string
    api_key = base64.urlsafe_b64encode(random_bytes).rstrip(b'=').decode('utf-8')
    return api_key


class GenaiCalendarAgentStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        
        sender_email = CfnParameter(self, 
                        "senderEmail", 
                        type="String", 
                        description="The sender email address.",
                        default="undefined" # Set default to undefined
                    )
        
        recipient_email = CfnParameter(self, 
                        "recipientEmail", 
                        type="String", 
                        description="The recipient email address.",
                        default="undefined" # Set default to undefined
                    )
        
        assistant_timezone = CfnParameter(self, 
                        "assistantTimezone", 
                        type="String", 
                        description="The timezone the AI assistant should use.",
                        default="Europe/Oslo" # Set default to Europe/Oslo
                    )

        prompt_generator_function = lambda_.Function(
            self, "prompt_generator", 
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="prompt_generator.lambda_handler",
            code=lambda_.Code.from_asset("./src/lambda/prompt_generator")
        )
        
        llm_output_parser_function = lambda_.Function(
            self, "llm_output_parser",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="llm_output_parser.lambda_handler",
            code=lambda_.Code.from_asset(
                "./src/lambda/llm_output_parser"
            ),
        )
        
        send_calendar_reminder_function = lambda_.Function(
            self,"send_calendar_reminder",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="send_calendar_reminder.lambda_handler",
            environment={
                "SENDER": sender_email.value_as_string,
                "RECIPIENT": recipient_email.value_as_string,
                "TIMEZONE": assistant_timezone.value_as_string
            },
            code=lambda_.Code.from_asset(
                "./src/lambda/send_calendar_reminder",
                bundling=core.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash", "-c",
                        "pip install --no-cache -r requirements.txt -t /asset-output && cp -au . /asset-output" #install needed pip package
                    ],
                ),
            ),
        )

        send_calendar_reminder_function.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
        )

        send_calendar_reminder_function.role.add_to_policy(
            iam.PolicyStatement(
                actions=["ses:SendRawEmail"],
                resources=["*"]
            )
        )
        
        
        # Define step function individual tasks
        generate_prompt_job = tasks.LambdaInvoke(
            self, "generate_prompt",
            lambda_function=prompt_generator_function,
            input_path="$.body", # as we are getting input from api gateway
            result_selector={
                "prompt_payload": sfn.JsonPath.string_at("$.Payload")
            }
        )


        model = bedrock.FoundationModel.from_foundation_model_id(self, "Model", bedrock.FoundationModelIdentifier.ANTHROPIC_CLAUDE_3_SONNET_20240229_V1_0)
        bedrock_extract_event_job = tasks.BedrockInvokeModel(self, "llm_extract_events",
            model=model,
            body=sfn.TaskInput.from_object({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 5000,
                "system.$": "$.prompt_payload.system_prompt",
                "messages.$": "$.prompt_payload.user_messages"
            }),
            result_selector={
                "completion.$": "$.Body"
            }
        )
        
        # a simple retry in case LLM did not return a valid json, try it again will most likely fix that
        bedrock_extract_event_job.add_retry(
            interval=Duration.seconds(5), 
            max_attempts=5,
            max_delay=Duration.seconds(10)
        )
                
        parse_llm_output_job = tasks.LambdaInvoke(
            self, "parse_llm_output",
            lambda_function=llm_output_parser_function,
            result_selector={
                "parsed_completion": sfn.JsonPath.string_at("$.Payload.parsed_completion")
            }
        )
        
        # For each extracted event, process them with its own logic 
        individual_event_map_job_container = sfn.Map(self, "individual_event_processor",
            max_concurrency=1,
            items_path=sfn.JsonPath.string_at("$.parsed_completion.function_calls")
        )
        
        choice_job_selector = sfn.Choice(self, "job_selector")
        
        job_selector_condition = sfn.Condition.string_equals("$.tool_name", "create-calendar-reminder")
        
        other_job_placeholder = sfn.Pass(
            self, "other_job_placeholder"
        )
        
        send_reminder_job = tasks.LambdaInvoke(
            self, "send_reminder_job",
            lambda_function=send_calendar_reminder_function, 
            input_path="$.parameters"
        )
        
        item_processor_chain = choice_job_selector.when(job_selector_condition, send_reminder_job).otherwise(other_job_placeholder).afterwards().next(sfn.Succeed(self, "Success"))
        
        individual_event_map_job_container.item_processor(item_processor_chain)
        
        chain = generate_prompt_job.next(bedrock_extract_event_job).next(parse_llm_output_job).next(individual_event_map_job_container)
        
        log_group = logs.LogGroup(self, "GenAI-Calendar-Assistant-StepFunction-LogGroup")
        
        state_machine = sfn.StateMachine(
            self, "GenAI-Calendar-Assistant",
            state_machine_type=sfn.StateMachineType.EXPRESS,
            definition_body=sfn.DefinitionBody.from_chainable(chain),
            tracing_enabled=True,
            logs=sfn.LogOptions(
                 destination=log_group,
                level=sfn.LogLevel.ALL,
                include_execution_data=True
            )
        )
        
        apigw_log_group = logs.LogGroup(self, "GenAI-Calendar-Assistant-ApiGatewayAccessLogs")
        apigw = apigateway.RestApi(self, 
                    "GenAI-Calendar-Assistant-APIGW",
                    cloud_watch_role=True, # to enable logging from the APIGW
                    deploy_options=apigateway.StageOptions(
                        access_log_destination=apigateway.LogGroupLogDestination(apigw_log_group),
                        access_log_format=apigateway.AccessLogFormat.clf(),
                        tracing_enabled=True,
                        metrics_enabled=True,
                        logging_level=apigateway.MethodLoggingLevel.ERROR
                    )
                )
        
        # Add request body schema to APIGW
        request_body_model = apigw.add_model(
            "RequestModel",
            content_type="application/json",
            model_name="RequestBodyModel",
            schema=apigateway.JsonSchema(
                schema=apigateway.JsonSchemaVersion.DRAFT4,
                title="RequestBodyModelSchema",
                type=apigateway.JsonSchemaType.OBJECT,
                properties={
                    "raw_body": {
                        "type": apigateway.JsonSchemaType.STRING
                    }
                },
                required=["raw_body"]
            )
        )
        
        #  Add request validator to APIGW
        my_request_body_validator = apigateway.RequestValidator(
            self, "MyRequestValidator",
            rest_api=apigw,
            request_validator_name="my_request_body_validator",
            validate_request_body=True
        )
        
        # Create API usage plan and API key
        usage_plan = apigw.add_usage_plan("UsagePlan",
            name="MyUsagePlanName",
            throttle=apigateway.ThrottleSettings(
                rate_limit=10,
                burst_limit=2
            )
        )
        
        generated_api_key_value = generate_api_key_base64(20)
        api_key = apigw.add_api_key("ApiKey",
            value=generated_api_key_value
        )
        usage_plan.add_api_key(api_key)
        
        # Create integration between APIGW and StepFunction 
        genai_post_method = apigw.root.add_method(
            http_method="POST", 
            request_models={
                "application/json": request_body_model
            },
            request_validator=my_request_body_validator,
            api_key_required=True,
            integration=apigateway.StepFunctionsIntegration.start_execution(state_machine)
        )
        
        # To associate a plan to a given RestAPI stage
        usage_plan.add_api_stage(
            stage=apigw.deployment_stage,
            throttle=[apigateway.ThrottlingPerMethod(
                method=genai_post_method,
                throttle=apigateway.ThrottleSettings(
                    rate_limit=10,
                    burst_limit=2
                )
            )
            ]
        )

        
        # Define cdk-nag suppression rules
        NagSuppressions.add_stack_suppressions(self, [
            dict(
                id = "AwsSolutions-IAM4",
                reason = "Suppressing error for uses AWS managed policies  AWSLambdaBasicExecutionRole for simple lambda function"
            ),
            dict(
                id = "AwsSolutions-IAM5",
                reason = "Suppressing error for containing wildcard permissions as it is from the managed AWSLambdaBasicExecutionRole for this simple lambda function"
            )
        ])

        NagSuppressions.add_resource_suppressions(apigw, [
            dict(
                id = "AwsSolutions-COG4",
                reason = "Suppressing error for not using Cognito user pool authorizer in this sample application. It is using a static API key for protecting the API Gateway. The value of the API key is generated during the deployment"
            ),
            dict(
                id = "AwsSolutions-APIG2",
                reason = "Suppressing false positive for not having request validation enabled - it does have request validation enabled for the POST method in the root resource"
            ),
            dict(
                id = "AwsSolutions-APIG3",
                reason = "Suppressing warning for not associated the API GW with AWS WAFv2 web ACL in this sample application"
            ),
            dict(
                id = "AwsSolutions-APIG4",
                reason = "Suppressing error for not implementing authorization in this sample application. It is using a static API key for protecting the API Gateway. The value of the API key is during during the deployment"
            )
            ],
            apply_to_children=True
        )
        
        #final cdk output for end user
        core.CfnOutput(self, "APIUrl", value=apigw.url)
        core.CfnOutput(self, "GeneratedAPIKeyValue", value=generated_api_key_value)

