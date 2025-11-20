"""
Dynamic Soda Core Configuration Generator
Discovers datasets and generates quality checks automatically based on data profiling
"""

import yaml
import pandas as pd
from sqlalchemy import create_engine, inspect, text
from typing import Dict, List, Any
import re
from datetime import datetime
import os


class SodaConfigGenerator:
    """
    Dynamically generates Soda Core configuration based on data discovery
    """
    
    def __init__(self, connection_string: str, data_source_name: str = "my_datasource"):
        """
        Initialize the generator with database connection
        
        Args:
            connection_string: SQLAlchemy connection string
            data_source_name: Name for the Soda data source
        """
        self.engine = create_engine(connection_string)
        self.inspector = inspect(self.engine)
        self.data_source_name = data_source_name
        self.discovered_metadata = {}
        
    def discover_datasets(self, schema: str = None, include_tables: List[str] = None, 
                         exclude_tables: List[str] = None) -> List[str]:
        """
        Discover all tables in the database
        
        Args:
            schema: Database schema to scan (optional)
            include_tables: List of specific tables to include (optional)
            exclude_tables: List of tables to exclude (optional)
            
        Returns:
            List of table names
        """
        all_tables = self.inspector.get_table_names(schema=schema)
        
        if include_tables:
            all_tables = [t for t in all_tables if t in include_tables]
        
        if exclude_tables:
            all_tables = [t for t in all_tables if t not in exclude_tables]
        
        print(f"✓ Discovered {len(all_tables)} tables")
        return all_tables
    
    def profile_table(self, table_name: str, schema: str = None, 
                     sample_size: int = 30000) -> Dict[str, Any]:
        """
        Profile a table to discover column types, patterns, and statistics
        
        Args:
            table_name: Name of the table to profile
            schema: Database schema
            sample_size: Number of rows to sample for profiling
            
        Returns:
            Dictionary with table metadata and column profiles
        """
        print(f"  Profiling table: {table_name}...")
        
        # Get column information
        columns = self.inspector.get_columns(table_name, schema=schema)
        pk_constraint = self.inspector.get_pk_constraint(table_name, schema=schema)
        primary_keys = pk_constraint.get('constrained_columns', [])
        
        # Get row count
        with self.engine.connect() as conn:
            count_query = f"SELECT COUNT(*) as cnt FROM {schema}.{table_name}"
            row_count = conn.execute(text(count_query)).fetchone()[0]
            
            # Sample data for profiling
            sample_query = f"SELECT * FROM {schema}.{table_name} LIMIT {sample_size}"
            df = pd.read_sql(sample_query, conn)
            
            # Handle blank tables
            if df.empty:
                print(f"  ⚠ Warning: {table_name} is empty, skipping detailed profiling")
                return
        
        # Profile each column
        column_profiles = []
        date_columns = []
        
        for col in columns:
            col_name = col['name']
            col_type = str(col['type']).lower()
            
            profile = {
                'name': col_name,
                'type': col_type,
                'nullable': col['nullable'],
                'is_primary_key': col_name in primary_keys,
                'checks': []
            }
            
            if col_name in df.columns:
                series = df[col_name]
                
                # Basic statistics
                profile['null_count'] = series.isna().sum()
                profile['null_percent'] = (profile['null_count'] / len(series)) * 100
                # Unique and duplicate handling - make values hashable first
                try:
                    # Convert unhashable list/dict/set objects into hashable forms
                    safe_series = series.apply(
                        lambda x: tuple(x) if isinstance(x, list) else (
                            tuple(sorted(x)) if isinstance(x, set) else (
                                tuple(sorted((k, v) for k, v in x.items())) if isinstance(x, dict) else x
                            )
                        )
                    )

                    profile['unique_count'] = int(safe_series.nunique(dropna=True))
                    profile['duplicate_percent'] = (
                        ((len(safe_series) - profile['unique_count']) / len(safe_series)) * 100
                        if len(safe_series) > 0 else 0
                    )
                except Exception:
                    # Fallback if something still fails
                    profile['unique_count'] = None
                    profile['duplicate_percent'] = None

                # Type-specific profiling
                if 'int' in col_type or 'numeric' in col_type or 'float' in col_type or 'decimal' in col_type:
                    profile['data_category'] = 'numeric'
                    profile['min'] = float(series.min()) if not series.isna().all() else None
                    profile['max'] = float(series.max()) if not series.isna().all() else None
                    profile['avg'] = float(series.mean()) if not series.isna().all() else None
                    profile['stddev'] = float(series.std()) if not series.isna().all() else None

                elif 'char' in col_type or 'text' in col_type or 'string' in col_type:
                    profile['data_category'] = 'text'
                    non_null = series.dropna().astype(str)
                    if len(non_null) > 0:
                        profile['min_length'] = non_null.str.len().min()
                        profile['max_length'] = non_null.str.len().max()
                        profile['avg_length'] = non_null.str.len().mean()

                        # Detect patterns
                        profile['patterns'] = self._detect_patterns(non_null)

                elif 'date' in col_type or 'timestamp' in col_type:
                    profile['data_category'] = 'datetime'
                    date_columns.append(col_name)
                    non_null = pd.to_datetime(series.dropna(), errors='coerce')
                    if len(non_null) > 0:
                        profile['min_date'] = non_null.min()
                        profile['max_date'] = non_null.max()

                elif 'bool' in col_type:
                    profile['data_category'] = 'boolean'
                    if not series.isna().all():
                        bool_series = series.fillna(False).astype(bool)
                        profile['true_count'] = bool_series.sum()
                        profile['false_count'] = (~bool_series).sum()
                    else:
                        profile['true_count'] = 0
                        profile['false_count'] = 0


                else:
                    profile['data_category'] = 'other'

                # Detect if column has categorical values (low cardinality)
                unique_count = profile.get('unique_count')

                if unique_count is not None and isinstance(unique_count, (int, float)) and 0 < unique_count <= 50:
                    profile['is_categorical'] = True
                    if col_name in df.columns:
                        # Reuse safe conversion to collect valid values without errors
                        safe_series = df[col_name].apply(
                            lambda x: tuple(x) if isinstance(x, list) else (
                                tuple(sorted(x)) if isinstance(x, set) else (
                                    tuple(sorted((k, v) for k, v in x.items())) if isinstance(x, dict) else x
                                )
                            )
                        )
                        vals = []
                        seen = set()
                        for v in safe_series.dropna().tolist():
                            key = v
                            if key not in seen:
                                seen.add(key)
                                vals.append(v)
                                if len(vals) >= 50:
                                    break
                        profile['valid_values'] = vals
                else:
                    profile['is_categorical'] = False

            
            column_profiles.append(profile)
        
        table_metadata = {
            'name': table_name,
            'row_count': row_count,
            'column_count': len(columns),
            'primary_keys': primary_keys,
            'columns': column_profiles,
            'date_columns': date_columns
        }
        
        return table_metadata
    
    def _detect_patterns(self, series: pd.Series) -> Dict[str, Any]:
        """Detect common patterns in text data"""
        patterns = {}
        sample = series.head(100)
        
        # Email pattern
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        email_matches = sample.str.match(email_pattern).sum()
        if email_matches / len(sample) > 0.8:
            patterns['type'] = 'email'
            patterns['confidence'] = email_matches / len(sample)
        
        # Phone pattern
        phone_pattern = r'^[\+]?[(]?[0-9]{1,4}[)]?[-\s\.]?[(]?[0-9]{1,4}[)]?[-\s\.]?[0-9]{1,9}$'
        phone_matches = sample.str.match(phone_pattern).sum()
        if phone_matches / len(sample) > 0.8:
            patterns['type'] = 'phone'
            patterns['confidence'] = phone_matches / len(sample)
        
        # UUID pattern
        uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        uuid_matches = sample.str.match(uuid_pattern, flags=re.IGNORECASE).sum()
        if uuid_matches / len(sample) > 0.8:
            patterns['type'] = 'uuid'
            patterns['confidence'] = uuid_matches / len(sample)
        
        # URL pattern
        url_pattern = r'^https?://[^\s]+$'
        url_matches = sample.str.match(url_pattern).sum()
        if url_matches / len(sample) > 0.8:
            patterns['type'] = 'url'
            patterns['confidence'] = url_matches / len(sample)
        
        return patterns
    
    def generate_checks_for_table(self, table_metadata: Dict[str, Any], 
                                  strict_mode: bool = False) -> List[Dict[str, Any]]:
        """
        Generate appropriate quality checks based on table profile
        
        Args:
            table_metadata: Profiled table metadata
            strict_mode: If True, generates stricter checks
            
        Returns:
            List of check definitions
        """
        if table_metadata is None:
            print(f"  ⚠ Warning: No metadata found for table, skipping checks")
            return []
        
        checks = []
        table_name = table_metadata['name']
        
        # TABLE-LEVEL CHECKS
        
        # Row count check
        if table_metadata['row_count'] > 0:
            checks.append({
                'row_count > 0': {
                    'name': f'Table {table_name} should not be empty'
                }
            })
            
            # Add change monitoring for tables with data
            checks.append({
                'change for row_count between -20% and 50%': {
                    'name': f'Monitor row count changes in {table_name}'
                }
            })
        
        # Schema check
        required_columns = [col['name'] for col in table_metadata['columns'] 
                          if col['is_primary_key'] or not col['nullable']]
        
        if required_columns:
            checks.append({
                'schema': {
                    'name': f'Validate {table_name} schema',
                    'warn': {
                        'when schema changes': ['column add', 'column delete', 'column type change']
                    },
                    'fail': {
                        'when required column missing': required_columns
                    }
                }
            })
        
        # Freshness check if date columns exist
        if table_metadata['date_columns']:
            most_recent_col = table_metadata['date_columns'][0]  # Use first date column
            checks.append({
                f'freshness({most_recent_col}) < 7d': {
                    'name': f'Data freshness check for {table_name}'
                }
            })
        
        # COLUMN-LEVEL CHECKS
        
        for col in table_metadata['columns']:
            col_name = col['name']
            
            # Primary key checks
            if col['is_primary_key']:
                checks.append({
                    f'missing_count({col_name}) = 0': {
                        'name': f'Primary key {col_name} cannot be null'
                    }
                })
                checks.append({
                    f'duplicate_count({col_name}) = 0': {
                        'name': f'Primary key {col_name} must be unique'
                    }
                })
            
            # Non-nullable column checks
            elif not col['nullable']:
                checks.append({
                    f'missing_count({col_name}) = 0': {
                        'name': f'Required field {col_name} cannot be null'
                    }
                })
            
            # Missing value checks for nullable columns with low null rate
            elif col.get('null_percent', 0) < 10:
                threshold = 15 if strict_mode else 25
                checks.append({
                    f'missing_percent({col_name}) < {threshold}%': {
                        'name': f'Missing values in {col_name} below threshold'
                    }
                })
            
            # Duplicate checks for columns with high uniqueness
            if col.get('unique_count', 0) / max(table_metadata['row_count'], 1) > 0.95:
                checks.append({
                    f'duplicate_percent({col_name}) < 5%': {
                        'name': f'Check uniqueness of {col_name}'
                    }
                })
            
            # Numeric column checks
            if col.get('data_category') == 'numeric':
                if col.get('min') is not None:
                    # Add range checks based on discovered values
                    min_val = col['min']
                    max_val = col['max']
                    
                    checks.append({
                        f'invalid_count({col_name}) = 0': {
                            'name': f'Validate {col_name} range',
                            'valid min': min_val * 0.9 if min_val > 0 else min_val * 1.1,
                            'valid max': max_val * 1.1 if max_val > 0 else max_val * 0.9
                        }
                    })
                    
                    # Statistical anomaly detection for numeric columns
                    if table_metadata['row_count'] > 1000:
                        checks.append({
                            f'anomaly detection for {col_name}': {
                                'name': f'Detect anomalies in {col_name}'
                            }
                        })
            
            # Text column checks
            elif col.get('data_category') == 'text':
                # Length validations
                if col.get('min_length'):
                    checks.append({
                        f'min_length({col_name}) >= {int(col["min_length"] * 0.5)}': {
                            'name': f'Minimum length check for {col_name}'
                        }
                    })
                
                if col.get('max_length'):
                    checks.append({
                        f'max_length({col_name}) <= {int(col["max_length"] * 1.5)}': {
                            'name': f'Maximum length check for {col_name}'
                        }
                    })
                
                # Pattern-based validations
                patterns = col.get('patterns', {})
                if patterns.get('type') == 'email':
                    checks.append({
                        f'invalid_count({col_name}) = 0': {
                            'name': f'Validate email format in {col_name}',
                            'valid format': 'email'
                        }
                    })
                
                elif patterns.get('type') == 'phone':
                    checks.append({
                        f'invalid_percent({col_name}) < 5%': {
                            'name': f'Validate phone format in {col_name}',
                            'valid format': 'phone number'
                        }
                    })
                
                elif patterns.get('type') == 'uuid':
                    checks.append({
                        f'invalid_count({col_name}) = 0': {
                            'name': f'Validate UUID format in {col_name}',
                            'valid format': 'uuid'
                        }
                    })
            
            # Categorical column checks
            if col.get('is_categorical') and col.get('valid_values'):
                valid_values = col['valid_values']
                # Convert to strings and handle different types
                valid_values_str = [str(v) for v in valid_values if v is not None and str(v) != 'nan']
                
                if valid_values_str and len(valid_values_str) <= 20:
                    checks.append({
                        f'invalid_count({col_name}) = 0': {
                            'name': f'Validate categorical values in {col_name}',
                            'valid values': valid_values_str
                        }
                    })
            
            # Date column checks
            if col.get('data_category') == 'datetime':
                checks.append({
                    f'invalid_count({col_name}) = 0': {
                        'name': f'Validate date format in {col_name}',
                        'valid format': 'date iso 8601'
                    }
                })
        
        return checks
    
    def generate_configuration(self, connection_config: Dict[str, str]) -> str:
        """
        Generate the Soda configuration.yml content
        
        Args:
            connection_config: Database connection parameters
            
        Returns:
            YAML configuration string
        """
        config = {
            f'data_source {self.data_source_name}': connection_config
        }
        
        return yaml.dump(config, default_flow_style=False, sort_keys=False)
    
    def generate_checks_yaml(self, tables: List[str], schema: str = None, 
                            strict_mode: bool = False) -> str:
        """
        Generate the complete checks.yml file dynamically
        
        Args:
            tables: List of table names to generate checks for
            schema: Database schema
            strict_mode: Generate stricter checks
            
        Returns:
            YAML checks configuration string
        """
        all_checks = {}
        
        print(f"\n🔍 Generating dynamic checks for {len(tables)} tables...")
        print("=" * 70)
        
        for table in tables:
            # Profile the table
            table_metadata = self.profile_table(table, schema)
            self.discovered_metadata[table] = table_metadata
            
            # Generate checks
            checks = self.generate_checks_for_table(table_metadata, strict_mode)
            
            all_checks[f'checks for {table}'] = checks
            
            print(f"  ✓ Generated {len(checks)} checks for {table}")
        
        print("=" * 70)
        print(f"✅ Check generation complete!\n")
        
        return yaml.dump(all_checks, default_flow_style=False, sort_keys=False, width=1000)
    
    def save_configurations(self, output_dir: str, connection_config: Dict[str, str],
                          tables: List[str], schema: str = None, strict_mode: bool = False):
        """
        Generate and save both configuration.yml and checks.yml files
        
        Args:
            output_dir: Directory to save configuration files
            connection_config: Database connection parameters
            tables: List of tables to generate checks for
            schema: Database schema
            strict_mode: Generate stricter checks
        """
        os.makedirs(output_dir, exist_ok=True)
        
        # Generate and save configuration.yml
        config_yaml = self.generate_configuration(connection_config)
        config_path = os.path.join(output_dir, 'configuration.yml')
        with open(config_path, 'w') as f:
            f.write(config_yaml)
        print(f"✓ Saved configuration to: {config_path}")
        
        # Generate and save checks.yml
        checks_yaml = self.generate_checks_yaml(tables, schema, strict_mode)
        checks_path = os.path.join(output_dir, 'checks.yml')
        with open(checks_path, 'w') as f:
            f.write(checks_yaml)
        print(f"✓ Saved checks to: {checks_path}")
        
        # Generate metadata report
        report_path = os.path.join(output_dir, 'discovery_report.txt')
        self.generate_discovery_report(report_path)
        print(f"✓ Saved discovery report to: {report_path}")
    
    def generate_discovery_report(self, output_path: str):
        """Generate a human-readable discovery report"""
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write("DATA DISCOVERY REPORT\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n\n")
            
            for table_name, metadata in self.discovered_metadata.items():
                f.write(f"\nTABLE: {table_name}\n")
                f.write("-" * 80 + "\n")

                # 🛑 Skip if metadata was not captured due to empty/error table
                if metadata is None:
                    f.write("[WARNING] No metadata collected (empty table or profiling error)\n\n")
                

                f.write(f"Total Rows: {metadata.get('row_count', 'N/A')}\n")
                f.write(f"Total Columns: {metadata.get('column_count', 'N/A')}\n")

                primary_keys = metadata.get('primary_keys') or []
                f.write(f"Primary Keys: {', '.join(primary_keys) if primary_keys else 'None'}\n")

                date_columns = metadata.get('date_columns') or []
                f.write(f"Date Columns: {', '.join(date_columns) if date_columns else 'None'}\n\n")

                f.write("COLUMN DETAILS:\n")
                for col in metadata.get('columns', []):
                    f.write(f"\n  Column: {col.get('name')}\n")
                    f.write(f"    Type: {col.get('type')}\n")
                    f.write(f"    Nullable: {col.get('nullable')}\n")
                    f.write(f"    Null %: {col.get('null_percent', 0):.2f}%\n")
                    f.write(f"    Unique Values: {col.get('unique_count', 'N/A')}\n")

                    if col.get('data_category') == 'numeric':
                        f.write(f"    Min: {col.get('min', 'N/A')}\n")
                        f.write(f"    Max: {col.get('max', 'N/A')}\n")
                        f.write(f"    Avg: {col.get('avg', 'N/A')}\n")

                    if col.get('is_categorical'):
                        f.write(f"    Categorical: Yes (Valid values: {len(col.get('valid_values', []))})\n")

                    if col.get('patterns'):
                        f.write(f"    Pattern Detected: {col['patterns'].get('type', 'Unknown')}\n")

                f.write("\n")


# ==============================================================================
# USAGE EXAMPLES
# ==============================================================================

def example_postgres():

    connection_string = "postgresql://postgres:admin@localhost:5432/postgres"

    generator = SodaConfigGenerator(
        connection_string=connection_string,
        data_source_name="postgres_prod"
    )

    # 👇 Use your actual schema here (NOT public)
    tables = generator.discover_datasets(schema='dqm',include_tables=['all_jobs_details_scrape'])

    connection_config = {
        'type': 'postgres',
        'host': 'localhost',
        'port': 5432,
        'username': 'postgres',
        'password': 'admin',
        'database': 'postgres',
        'schema': 'dqm'   # 👈 Must match
    }

    generator.save_configurations(
        output_dir='./soda_config',
        connection_config=connection_config,
        tables=tables,
        schema='dqm',      # 👈 Must match
        strict_mode=False
    )



def example_snowflake():
    """Example: Generate configuration for Snowflake"""
    
    connection_string = "snowflake://user:password@account/database/schema?warehouse=warehouse"
    
    generator = SodaConfigGenerator(
        connection_string=connection_string,
        data_source_name="snowflake_prod"
    )
    
    tables = generator.discover_datasets(schema='PUBLIC')
    
    connection_config = {
        'type': 'snowflake',
        'account': 'my_account',
        'username': '${SNOWFLAKE_USER}',
        'password': '${SNOWFLAKE_PASSWORD}',
        'database': 'MY_DATABASE',
        'warehouse': 'MY_WAREHOUSE',
        'role': 'MY_ROLE',
        'schema': 'PUBLIC'
    }
    
    generator.save_configurations(
        output_dir='./soda_config',
        connection_config=connection_config,
        tables=tables,
        schema='PUBLIC',
        strict_mode=True
    )


def example_mysql():
    """Example: Generate configuration for MySQL"""
    
    connection_string = "mysql+pymysql://user:password@localhost:3306/mydb"
    
    generator = SodaConfigGenerator(
        connection_string=connection_string,
        data_source_name="mysql_prod"
    )
    
    tables = generator.discover_datasets()
    
    connection_config = {
        'type': 'mysql',
        'host': 'localhost',
        'port': 3306,
        'username': '${MYSQL_USER}',
        'password': '${MYSQL_PASSWORD}',
        'database': 'mydb'
    }
    
    generator.save_configurations(
        output_dir='./soda_config',
        connection_config=connection_config,
        tables=tables,
        strict_mode=False
    )


if __name__ == "__main__":
    """
    Run the appropriate example based on your database
    """
    
    print("""
    ╔══════════════════════════════════════════════════════════════════════╗
    ║         Dynamic Soda Core Configuration Generator                    ║
    ║                                                                       ║
    ║  Automatically discovers tables and generates quality checks         ║
    ║  based on data profiling and pattern detection                       ║
    ╚══════════════════════════════════════════════════════════════════════╝
    """)
    
    # Uncomment the example for your database type
    
    example_postgres()
    # example_snowflake()
    # example_mysql()
    
    print("""
    
    📋 To use this script:
    
    1. Install required packages:
       pip install sqlalchemy pandas pyyaml pymysql psycopg2-binary snowflake-sqlalchemy
    
    2. Update connection string in the appropriate example function
    
    3. Uncomment and run the example for your database type
    
    4. The script will generate:
       - configuration.yml (connection config)
       - checks.yml (dynamic quality checks)
       - discovery_report.txt (data profiling report)
    
    5. Run Soda scan:
       soda scan -d your_datasource -c configuration.yml checks.yml
    
    """)