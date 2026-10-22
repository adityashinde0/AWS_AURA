import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import {
  aws_kms as kms,
  aws_dynamodb as dynamodb,
  aws_sqs as sqs,
  aws_events as events,
  aws_events_targets as targets,
  aws_lambda as lambda,
  aws_iam as iam,
  aws_apigatewayv2 as apigw2,
  aws_apigatewayv2_integrations as integrations,
  aws_apigatewayv2_authorizers as authorizers,
  aws_cognito as cognito,
  aws_wafv2 as wafv2,
} from 'aws-cdk-lib';

export class AuraCloudIntakeStack extends cdk.Stack {
  public readonly kmsKey: kms.Key;
  public readonly coreTable: dynamodb.Table;
  public readonly wsConnectionsTable: dynamodb.Table;
  public readonly intakeQueue: sqs.Queue;
  public readonly intakeDlq: sqs.Queue;
  public readonly eventBus: events.EventBus;
  public readonly focusGuardianFunction: lambda.Function;
  public readonly wsConnectFunction: lambda.Function;
  public readonly wsRelayFunction: lambda.Function;
  public readonly controlPlaneFunction: lambda.Function;
  public readonly httpApi: apigw2.HttpApi;
  public readonly userPool?: cognito.UserPool;
  public readonly userPoolClient?: cognito.UserPoolClient;
  public readonly webAcl: wafv2.CfnWebACL;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // =========================================================================
    // 1. KMS Customer Managed Encryption Key (CMK)
    // =========================================================================
    this.kmsKey = new kms.Key(this, 'AuraEncryptionKey', {
      alias: 'alias/aura-kms-key',
      description: 'Customer Managed Key for Project AURA data encryption at rest',
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // =========================================================================
    // 2. DynamoDB Single-Table Knowledge Graph & Connection State
    // =========================================================================
    this.coreTable = new dynamodb.Table(this, 'AuraCoreTable', {
      tableName: 'AuraCoreTable',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.kmsKey,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.coreTable.addGlobalSecondaryIndex({
      indexName: 'GSI1',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.wsConnectionsTable = new dynamodb.Table(this, 'AuraWebSocketConnectionsTable', {
      tableName: 'AuraWebSocketConnections',
      partitionKey: { name: 'connectionId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.kmsKey,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // =========================================================================
    // 3. SQS Buffering & Dead Letter Queues (DLQs)
    // =========================================================================
    this.intakeDlq = new sqs.Queue(this, 'AuraIntakeDlq', {
      queueName: 'aura-intake-dlq',
      retentionPeriod: cdk.Duration.days(14),
      encryptionMasterKey: this.kmsKey,
    });

    this.intakeQueue = new sqs.Queue(this, 'AuraIntakeQueue', {
      queueName: 'aura-intake-queue',
      visibilityTimeout: cdk.Duration.seconds(30),
      encryptionMasterKey: this.kmsKey,
      deadLetterQueue: {
        queue: this.intakeDlq,
        maxReceiveCount: 3,
      },
    });

    const eventBridgeDlq = new sqs.Queue(this, 'AuraEventBridgeDlq', {
      queueName: 'aura-eventbridge-dlq',
      retentionPeriod: cdk.Duration.days(14),
      encryptionMasterKey: this.kmsKey,
    });

    // =========================================================================
    // 4. EventBridge Custom Bus & Routing Rules
    // =========================================================================
    this.eventBus = new events.EventBus(this, 'AuraEventBus', {
      eventBusName: 'aura-event-bus',
    });

    // Event Archive for compliant replay (retains privacy-safe metadata only)
    new events.Archive(this, 'AuraEventArchive', {
      archiveName: 'aura-event-archive',
      sourceEventBus: this.eventBus,
      retention: cdk.Duration.days(30),
      eventPattern: {
        source: ['aura.companion', 'aura.scheduler'],
      },
    });

    // =========================================================================
    // 5. Lambda Functions (Cloud Intake Handlers)
    // =========================================================================
    const lambdaDir = path.resolve(__dirname, '../../lambda');

    // 5a. Focus Guardian Lambda
    this.focusGuardianFunction = new lambda.Function(this, 'FocusGuardianFunction', {
      functionName: 'aura-focus-guardian',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'focus_guardian.lambda_handler',
      code: lambda.Code.fromAsset(lambdaDir),
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      environment: {
        AURA_CORE_TABLE: this.coreTable.tableName,
        AURA_EVENT_BUS: this.eventBus.eventBusName,
        AURA_KMS_KEY_ARN: this.kmsKey.keyArn,
        BEDROCK_MODEL_ID: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
        AURA_MODEL_ADAPTER: process.env.AURA_MODEL_ADAPTER || 'gateway',
        WATSONX_SECRET_ARN: process.env.WATSONX_SECRET_ARN || `arn:aws:secretsmanager:${this.region}:${this.account}:secret:aura/watsonx/credentials`,
        AURA_STORAGE_MODE: process.env.AURA_STORAGE_MODE || 'dynamodb',
      },
    });

    // 5b. WebSocket Connect & Disconnect Handler
    this.wsConnectFunction = new lambda.Function(this, 'WebSocketConnectFunction', {
      functionName: 'aura-ws-connect-handler',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'websocket_handler.lambda_handler',
      code: lambda.Code.fromAsset(lambdaDir),
      timeout: cdk.Duration.seconds(5),
      environment: {
        AURA_CONNECTIONS_TABLE: this.wsConnectionsTable.tableName,
      },
    });

    // 5c. WebSocket Relay Handler (Outbox ingestion & ACK)
    this.wsRelayFunction = new lambda.Function(this, 'WebSocketRelayFunction', {
      functionName: 'aura-ws-relay-handler',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'websocket_handler.lambda_handler',
      code: lambda.Code.fromAsset(lambdaDir),
      timeout: cdk.Duration.seconds(5),
      environment: {
        AURA_EVENT_BUS: this.eventBus.eventBusName,
      },
    });

    // 5d. HTTP Control Plane Handler
    this.controlPlaneFunction = new lambda.Function(this, 'ControlPlaneFunction', {
      functionName: 'aura-control-plane',
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'control_plane.lambda_handler',
      code: lambda.Code.fromAsset(lambdaDir),
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      environment: {
        AURA_CORE_TABLE: this.coreTable.tableName,
        AURA_STORAGE_MODE: process.env.AURA_STORAGE_MODE || 'dynamodb',
      },
    });

    // =========================================================================
    // 6. IAM Least-Privilege Policies
    // =========================================================================
    this.kmsKey.grantEncryptDecrypt(this.focusGuardianFunction);
    this.kmsKey.grantEncryptDecrypt(this.controlPlaneFunction);
    this.kmsKey.grantEncryptDecrypt(this.wsRelayFunction);

    this.coreTable.grantReadWriteData(this.focusGuardianFunction);
    this.coreTable.grantReadWriteData(this.controlPlaneFunction);
    this.wsConnectionsTable.grantReadWriteData(this.wsConnectFunction);

    this.eventBus.grantPutEventsTo(this.wsRelayFunction);
    this.eventBus.grantPutEventsTo(this.focusGuardianFunction);

    // Bedrock InvokeModel least-privilege grant
    this.focusGuardianFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'BedrockInvokeModelAccess',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          `arn:aws:bedrock:${this.region}::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0`,
          `arn:aws:bedrock:${this.region}::foundation-model/anthropic.claude-3-haiku-20240307-v1:0`,
        ],
      })
    );

    // Secrets Manager access for secondary provider credentials (IBM watsonx)
    this.focusGuardianFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'SecretsManagerWatsonxAccess',
        effect: iam.Effect.ALLOW,
        actions: ['secretsmanager:GetSecretValue'],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:aura/watsonx/*`,
        ],
      })
    );

    // Wire EventBridge rules to Focus Guardian Lambda
    new events.Rule(this, 'FocusSignalRule', {
      ruleName: 'aura-focus-signal-rule',
      eventBus: this.eventBus,
      eventPattern: {
        source: ['aura.companion'],
        detailType: ['FocusSignalDetected'],
      },
      targets: [
        new targets.LambdaFunction(this.focusGuardianFunction, {
          deadLetterQueue: eventBridgeDlq,
          maxEventAge: cdk.Duration.hours(2),
          retryAttempts: 2,
        }),
      ],
    });

    new events.Rule(this, 'MeetingPrepRule', {
      ruleName: 'aura-meeting-prep-rule',
      eventBus: this.eventBus,
      eventPattern: {
        source: ['aura.scheduler'],
        detailType: ['MeetingPrepTriggered'],
      },
      targets: [
        new targets.LambdaFunction(this.focusGuardianFunction, {
          deadLetterQueue: eventBridgeDlq,
          maxEventAge: cdk.Duration.hours(1),
          retryAttempts: 2,
        }),
      ],
    });

    // =========================================================================
    // 7. API Gateway HTTP Control Plane (v2)
    // =========================================================================
    this.httpApi = new apigw2.HttpApi(this, 'AuraHttpControlPlaneApi', {
      apiName: 'AuraHttpControlPlaneApi',
      description: 'Project AURA REST Control Plane API',
      corsPreflight: {
        allowHeaders: ['Authorization', 'Content-Type', 'X-Amz-Date', 'X-Api-Key'],
        allowMethods: [
          apigw2.CorsHttpMethod.GET,
          apigw2.CorsHttpMethod.POST,
          apigw2.CorsHttpMethod.DELETE,
          apigw2.CorsHttpMethod.OPTIONS,
        ],
        allowOrigins: ['*'],
      },
    });

    const controlPlaneIntegration = new integrations.HttpLambdaIntegration(
      'ControlPlaneIntegration',
      this.controlPlaneFunction
    );

    const focusGuardianIntegration = new integrations.HttpLambdaIntegration(
      'FocusGuardianHttpIntegration',
      this.focusGuardianFunction
    );

    // Cognito User Pool & HTTP API Authorizer
    const userPool = new cognito.UserPool(this, 'AuraUserPool', {
      userPoolName: 'aura-user-pool',
      selfSignUpEnabled: false,
      signInAliases: { email: true, username: true },
      standardAttributes: {
        email: { required: true, mutable: true },
      },
      passwordPolicy: {
        minLength: 12,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const userPoolClient = new cognito.UserPoolClient(this, 'AuraUserPoolClient', {
      userPool: userPool,
      userPoolClientName: 'aura-web-client',
      generateSecret: false,
      authFlows: {
        userSrp: true,
      },
    });

    const cognitoAuthorizer = new authorizers.HttpUserPoolAuthorizer(
      'AuraCognitoAuthorizer',
      userPool,
      {
        userPoolClients: [userPoolClient],
        identitySource: ['$request.header.Authorization'],
      }
    );

    // =========================================================================
    // REST Route Inventory
    // =========================================================================

    // --- Public Routes (No Authorizer) ---
    this.httpApi.addRoutes({
      path: '/health',
      methods: [apigw2.HttpMethod.GET],
      integration: controlPlaneIntegration,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/health',
      methods: [apigw2.HttpMethod.GET],
      integration: controlPlaneIntegration,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/devices',
      methods: [apigw2.HttpMethod.GET],
      integration: controlPlaneIntegration,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/feature-flags',
      methods: [apigw2.HttpMethod.GET],
      integration: controlPlaneIntegration,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/slack/oauth/callback',
      methods: [apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
    });

    // --- Protected Routes (Cognito JWT Authorizer) ---
    this.httpApi.addRoutes({
      path: '/api/v1/focus/signal',
      methods: [apigw2.HttpMethod.POST],
      integration: focusGuardianIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/focus/session',
      methods: [apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/focus/bank',
      methods: [apigw2.HttpMethod.GET],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/focus/signals',
      methods: [apigw2.HttpMethod.GET, apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/consent/grants',
      methods: [apigw2.HttpMethod.GET, apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/consent/grants/{grantId}',
      methods: [apigw2.HttpMethod.DELETE],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/devices',
      methods: [apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/devices/wearable',
      methods: [apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/apns/register',
      methods: [apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/feature-flags',
      methods: [apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/safety/guardrails',
      methods: [apigw2.HttpMethod.GET],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/safety/guardrails/reset',
      methods: [apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/safety/escalate',
      methods: [apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/safety/cancel',
      methods: [apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/audit/actions',
      methods: [apigw2.HttpMethod.GET],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/actions/undo',
      methods: [apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/preferences',
      methods: [apigw2.HttpMethod.GET, apigw2.HttpMethod.POST, apigw2.HttpMethod.PATCH],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/routines',
      methods: [apigw2.HttpMethod.GET, apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/routines/{routineId}',
      methods: [apigw2.HttpMethod.DELETE, apigw2.HttpMethod.PUT, apigw2.HttpMethod.PATCH],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/slack/oauth/authorize',
      methods: [apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/slack/preview',
      methods: [apigw2.HttpMethod.GET],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/slack/status',
      methods: [apigw2.HttpMethod.POST, apigw2.HttpMethod.DELETE],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/slack/workspaces',
      methods: [apigw2.HttpMethod.GET],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/slack/admin-review',
      methods: [apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/notifications/feedback',
      methods: [apigw2.HttpMethod.POST],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/user/export',
      methods: [apigw2.HttpMethod.GET],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    this.httpApi.addRoutes({
      path: '/api/v1/user/data',
      methods: [apigw2.HttpMethod.DELETE],
      integration: controlPlaneIntegration,
      authorizer: cognitoAuthorizer,
    });

    // =========================================================================
    // 8. API Gateway WebSocket Ingress API
    // =========================================================================
    const cfnWebSocketApi = new apigw2.CfnApi(this, 'AuraWebSocketApi', {
      name: 'AuraWebSocketIntakeApi',
      protocolType: 'WEBSOCKET',
      routeSelectionExpression: '$request.body.action',
      description: 'Project AURA WebSocket Intake API for real-time local companion relay',
    });

    // Allow API Gateway to invoke WebSocket lambdas
    const wsApiArn = `arn:aws:execute-api:${this.region}:${this.account}:${cfnWebSocketApi.ref}/*`;

    this.wsConnectFunction.addPermission('WsConnectInvokePermission', {
      principal: new iam.ServicePrincipal('apigateway.amazonaws.com'),
      sourceArn: wsApiArn,
    });

    this.wsRelayFunction.addPermission('WsRelayInvokePermission', {
      principal: new iam.ServicePrincipal('apigateway.amazonaws.com'),
      sourceArn: wsApiArn,
    });

    const connectIntegration = new apigw2.CfnIntegration(this, 'WsConnectIntegration', {
      apiId: cfnWebSocketApi.ref,
      integrationType: 'AWS_PROXY',
      integrationUri: `arn:aws:apigateway:${this.region}:lambda:path/2015-03-31/functions/${this.wsConnectFunction.functionArn}/invocations`,
    });

    const relayIntegration = new apigw2.CfnIntegration(this, 'WsRelayIntegration', {
      apiId: cfnWebSocketApi.ref,
      integrationType: 'AWS_PROXY',
      integrationUri: `arn:aws:apigateway:${this.region}:lambda:path/2015-03-31/functions/${this.wsRelayFunction.functionArn}/invocations`,
    });

    new apigw2.CfnRoute(this, 'WsConnectRoute', {
      apiId: cfnWebSocketApi.ref,
      routeKey: '$connect',
      authorizationType: 'NONE',
      target: `integrations/${connectIntegration.ref}`,
    });

    new apigw2.CfnRoute(this, 'WsDisconnectRoute', {
      apiId: cfnWebSocketApi.ref,
      routeKey: '$disconnect',
      authorizationType: 'NONE',
      target: `integrations/${connectIntegration.ref}`,
    });

    new apigw2.CfnRoute(this, 'WsRelayRoute', {
      apiId: cfnWebSocketApi.ref,
      routeKey: 'relay',
      authorizationType: 'NONE',
      target: `integrations/${relayIntegration.ref}`,
    });

    new apigw2.CfnRoute(this, 'WsDefaultRoute', {
      apiId: cfnWebSocketApi.ref,
      routeKey: '$default',
      authorizationType: 'NONE',
      target: `integrations/${relayIntegration.ref}`,
    });

    const wsStage = new apigw2.CfnStage(this, 'WsStageProd', {
      apiId: cfnWebSocketApi.ref,
      stageName: 'prod',
      autoDeploy: true,
    });

    // =========================================================================
    // 9. AWS WAF Web Application Firewall
    // =========================================================================
    this.webAcl = new wafv2.CfnWebACL(this, 'AuraWebAcl', {
      name: 'AuraCloudIntakeWebAcl',
      scope: 'REGIONAL',
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: 'AuraCloudIntakeWebAclMetric',
        sampledRequestsEnabled: true,
      },
      rules: [
        {
          name: 'AWS-AWSManagedRulesCommonRuleSet',
          priority: 10,
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesCommonRuleSet',
            },
          },
          overrideAction: { none: {} },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'AWSCommonRuleSetMetric',
            sampledRequestsEnabled: true,
          },
        },
        {
          name: 'AWS-AWSManagedRulesKnownBadInputsRuleSet',
          priority: 20,
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesKnownBadInputsRuleSet',
            },
          },
          overrideAction: { none: {} },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'AWSKnownBadInputsMetric',
            sampledRequestsEnabled: true,
          },
        },
        {
          name: 'RateLimit500Per5Min',
          priority: 30,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: 500,
              aggregateKeyType: 'IP',
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'RateLimitMetric',
            sampledRequestsEnabled: true,
          },
        },
      ],
    });

    // Stack Outputs
    new cdk.CfnOutput(this, 'KmsKeyArnOutput', {
      value: this.kmsKey.keyArn,
      exportName: 'AuraKmsKeyArn',
    });

    new cdk.CfnOutput(this, 'CoreTableNameOutput', {
      value: this.coreTable.tableName,
      exportName: 'AuraCoreTableName',
    });

    new cdk.CfnOutput(this, 'HttpApiEndpointOutput', {
      value: this.httpApi.apiEndpoint,
      exportName: 'AuraHttpApiEndpoint',
    });

    new cdk.CfnOutput(this, 'WebSocketApiEndpointOutput', {
      value: `wss://${cfnWebSocketApi.ref}.execute-api.${this.region}.amazonaws.com/${wsStage.stageName}`,
      exportName: 'AuraWebSocketApiEndpoint',
    });

    new cdk.CfnOutput(this, 'EventBusArnOutput', {
      value: this.eventBus.eventBusArn,
      exportName: 'AuraEventBusArn',
    });
  }
}
