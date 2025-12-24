# Schema for validating the classification configuration file.
# V 1.0
CLASSIFICATION_SCHEMA = {
    'config_type': {'type': 'string','required': True,'allowed': ["Classification"] },
    'tiers': {
        'type': 'dict', 'valuesrules': {
            'type': 'dict', 'schema': {
                'separation': {'type': 'number', 'required': False},
                'above_pctl': {'type': 'number', 'min': 0, 'max': 100, 'required': False},
                'below_pctl': {'type': 'number', 'min': 0, 'max': 100, 'required': False},
                'style_weight': {'type': 'string', 'required': False},
            }
        }
    },
    'score_key': {'type': 'string', 'required': True},
    'anomaly_group_key': {'type': 'string', 'required': False},
    'style': {'type': 'string', 'required': False},
    'features': {'type': 'list', 'required': True},
    'require_name': {
        'type': 'boolean',
        'required': False,
        'default': True
    },
    'debug_ids': {
        'type': 'list', 'schema': {'type': 'integer'},
        'required': False,
    },
    'enrichment': {
        'type': 'list',
        'required': False,
        'schema': {
            'type': 'dict',
            'schema': {
                'file_suffix': {'type': 'string', 'required': True},
                'columns': {
                    'type': 'list',
                    'required': True,
                    'schema': {'type': 'string'}
                }
            }
        }
    },
    'keys': {'type': 'dict', 'required': True},
    'output_tag': {'type': 'string', 'required': True},
}