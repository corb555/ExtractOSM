FEATURE_DEFINITION_SCHEMA = {
    'type': 'dict', 'schema': {
        # Extraction representation used by ExtractOSM.
        'mode': {
            'type': 'string', 'required': True, 'allowed': ['value', 'asis', 'presence', 'score'],
        },
    }
}

FEATURES_CONFIG_SCHEMA = {
    'config_type': {'type': 'string', 'required': True, 'allowed': ["Features"]},
    'id_column': {'type': 'string', 'required': True},

    # OSM extraction/classification rules that determine which source records and
    # base columns become part of the category feature table.
    'osm_filter': {'type': 'dict', 'required': True},

    'require_name': {
        'type': 'boolean', 'required': False, 'default': True
    },

    'debug_ids': {
        'type': 'list', 'schema': {'type': 'integer'}, 'required': False,
    },

    # External enrichment files merged into the canonical feature CSV.
    'enrichment': {
        'type': 'list', 'required': False, 'schema': {
            'type': 'dict', 'schema': {
                'file_suffix': {'type': 'string', 'required': True}, 'columns': {
                    'type': 'list', 'required': True, 'schema': {'type': 'string'}
                }
            }
        }
    },

    # Canonical feature-table columns. Feature construction owns this list;
    # coefficients and other scoring policy belong in MODEL_CONFIG_SCHEMA.
    'features': {
        'type': 'list', 'required': True, 'schema': {
            'type': 'dict', 'keysrules': {'type': 'string'},
            'valuesrules': FEATURE_DEFINITION_SCHEMA,
        }
    },

    'style': {'type': 'string', 'required': False},
}

# TODO remove old name
CLASSIFICATION_SCHEMA = FEATURES_CONFIG_SCHEMA
