# Example usage of the complete DAG system

import asyncio
from pathlib import Path
from core.dag import (
    YAMLDAGBuilder, 
    CodeGenerator, 
    TemplateLibrary,
    DAGVisualizer,
    DAGRuntime
)

# Example YAML workflow
yaml_content = """
name: data_analysis_pipeline
version: "1.0"
description: "Complete data analysis workflow with ML"
start_state: fetch_data
end_states: [notify_complete]

states:
  fetch_data:
    type: task
    config:
      plugin: "s3_reader"
      bucket: "data-bucket"
      prefix: "raw-data/"
    transitions:
      - validate_schema
    resources:
      cpu: 2
      memory: 2048
    metadata:
      asset: "raw_data"
      description: "Fetch raw data from S3"

  validate_schema:
    type: task
    config:
      plugin: "schema_validator"
      schema_file: "schemas/data.json"
    dependencies: [fetch_data]
    transitions:
      - target: clean_data
        condition: "validation.passed"
      - target: quarantine_data
        condition: "not validation.passed"

  clean_data:
    type: task
    config:
      plugin: "data_cleaner"
      operations:
        - remove_duplicates
        - handle_missing
        - normalize_values
    dependencies: [validate_schema]
    transitions:
      - feature_engineering

  feature_engineering:
    type: parallel
    config:
      tasks:
        - numerical_features
        - categorical_features
        - text_features
    dependencies: [clean_data]
    transitions:
      - merge_features

  numerical_features:
    type: task
    config:
      plugin: "feature_extractor"
      feature_type: "numerical"
    metadata:
      asset: "numerical_features"

  categorical_features:
    type: task
    config:
      plugin: "feature_extractor"
      feature_type: "categorical"
    metadata:
      asset: "categorical_features"

  text_features:
    type: task
    config:
      plugin: "feature_extractor"
      feature_type: "text"
      model: "bert-base"
    resources:
      gpu: 1
    metadata:
      asset: "text_features"

  merge_features:
    type: task
    config:
      plugin: "feature_merger"
    dependencies:
      - numerical_features
      - categorical_features
      - text_features
    transitions:
      - train_model
    metadata:
      asset: "feature_matrix"

  train_model:
    type: task
    config:
      plugin: "ml_trainer"
      algorithm: "xgboost"
      hyperparameters:
        n_estimators: 100
        max_depth: 6
        learning_rate: 0.1
    dependencies: [merge_features]
    transitions:
      - evaluate_model
    resources:
      cpu: 8
      memory: 16384
      gpu: 2
    metadata:
      asset: "trained_model"

  evaluate_model:
    type: task
    config:
      plugin: "model_evaluator"
      metrics: ["accuracy", "precision", "recall", "f1", "auc"]
      threshold: 0.85
    dependencies: [train_model]
    transitions:
      - target: deploy_model
        condition: "metrics.f1 >= 0.85"
      - target: log_results
        condition: "metrics.f1 < 0.85"

  deploy_model:
    type: task
    config:
      plugin: "model_deployer"
      endpoint: "https://api.example.com/models/v1"
      canary_percentage: 10
    dependencies: [evaluate_model]
    transitions:
      - notify_complete

  quarantine_data:
    type: task
    config:
      plugin: "data_quarantine"
      location: "s3://data-bucket/quarantine/"
    transitions:
      - notify_complete

  log_results:
    type: task
    config:
      plugin: "result_logger"
      destination: "mlflow"
    transitions:
      - notify_complete

  notify_complete:
    type: task
    config:
      plugin: "notifier"
      channels: ["slack", "email"]
      message: "Pipeline completed: {{ workflow.name }}"
"""

async def main():
    # 1. Build DAG from YAML
    builder = YAMLDAGBuilder()
    workflow, dag = builder.build_from_yaml_string(yaml_content)
    
    print(f"Built DAG with {len(dag._nodes)} nodes")
    
    # 2. Validate DAG
    dag.validate()
    print("DAG validation passed")
    
    # 3. Visualize DAG
    visualizer = DAGVisualizer()
    
    # Generate Graphviz
    dot_source = visualizer.to_dot(dag)
    print("\nGraphviz representation:")
    print(dot_source[:200] + "...")
    
    # Generate Mermaid
    mermaid = visualizer.to_mermaid(dag)
    print("\nMermaid diagram:")
    print(mermaid[:200] + "...")
    
    # 4. Generate Python code
    generator = CodeGenerator()
    
    # Generate workflow code
    workflow_code = generator.generate_workflow_code(workflow)
    print("\nGenerated Python code:")
    print(workflow_code[:500] + "...")
    
    # Save to file
    output_dir = Path("generated")
    output_dir.mkdir(exist_ok=True)
    
    code_file = output_dir / f"{workflow.name}.py"
    generator.generate_workflow_code(workflow, code_file)
    
    # Generate tests
    test_file = output_dir / f"test_{workflow.name}.py"
    generator.generate_test_code(workflow, test_file)
    
    # 5. Use templates
    library = TemplateLibrary()
    
    # List available templates
    print("\nAvailable templates:")
    for template_name in library.list_templates():
        print(f"  - {template_name}")
    
    # Render ETL template
    etl_workflow = library.render_template(
        "etl_pipeline",
        name="customer_etl",
        source_url="s3://data/customers.csv",
        destination_url="redshift://analytics/customers"
    )
    
    print(f"\nRendered ETL workflow: {etl_workflow.name}")
    
    # 6. Execute DAG
    runtime = DAGRuntime()
    
    print("\nExecuting workflow...")
    execution_context = await runtime.execute_workflow(workflow, dag)
    
    print(f"Execution completed in {
        (execution_context.end_time - execution_context.start_time).total_seconds()
    } seconds")
    
    # Print execution summary
    successful = sum(
        1 for exec in execution_context.node_executions.values()
        if exec.status.value == "success"
    )
    print(f"Successful nodes: {successful}/{len(dag._nodes)}")

if __name__ == "__main__":
    asyncio.run(main())